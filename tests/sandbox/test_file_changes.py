import asyncio
import json
import os
from pathlib import Path
import stat
from unittest.mock import patch

import pytest

from redpanda.sandbox.files import WorkspaceFiles
from redpanda.sandbox.files.changes import attribution
from redpanda.sandbox.files.file_view import native
from redpanda.sandbox.files.file_view.publication import image, save_block


class Evidence:
    def __init__(self, tmp_path):
        self.root, self.store = tmp_path / "project", tmp_path / "store"
        self.root.mkdir()
        (self.store / "cas").mkdir(parents=True)
        self.path = self.root / "file"
        self.path.write_bytes(b"AAAAAA")
        with self.path.open("rb") as file:
            self.identity = native.identity(file)
        self.active = []

    def file(self, data, *, complete=True):
        blocks = {str(i // 65536): save_block(self.store, data[i:i + 65536])
                  for i in range(0, len(data), 65536)}
        if not complete:
            blocks = {"0": blocks["0"]}
        return image("file", len(data), self.identity, complete=complete, blocks=blocks,
                     mode=None if os.name == "nt" else stat.S_IMODE(self.path.stat().st_mode))

    def operation(self, before, after, *, published=True, restore=False):
        identity = f"{len(self.active) + 1:064x}" if published else "f" * 64
        folder = self.store / "commands" / identity
        folder.mkdir(parents=True)
        (folder / "sealed.json").write_text(json.dumps({
            "version": 3, "command_id": identity, "parent_commit": None, "digest": "a" * 64,
            "changes": [{"path": "/file", "before": before, "after": after}],
            "operation": {"kind": "restore", "targets": self.active[:], "policy": "preserve"} if restore else {"kind": "execute"},
        }), encoding="utf-8")
        if published:
            self.active.append(identity)
        (self.store / "host-index.json").write_text(json.dumps({
            "version": 3, "active": self.active, "bindings": {"/file": self.identity},
        }), encoding="utf-8")

    def state(self):
        return attribution(self.root, self.store, self.path)["changes"][0]


def test_interleaved_external_edits_are_not_absorbed_into_later_agent_changes(tmp_path):
    evidence = Evidence(tmp_path)
    evidence.operation(evidence.file(b"AAAAAA"), evidence.file(b"BAAAAA"))
    evidence.operation(evidence.file(b"BAHAAA"), evidence.file(b"BAHAAC"))
    evidence.path.write_bytes(b"BUHAAC")
    state = evidence.state()
    assert state["origin"] == "mixed"
    assert state["edits"][0]["before"] == "AAAAAA"
    assert state["edits"][0]["after"] == "BUHAAC"
    assert state["edits"][0]["origin"] == "mixed"
    assert state["content_complete"]


def test_restoration_removes_agent_results_and_keeps_external_values(tmp_path):
    evidence = Evidence(tmp_path)
    evidence.operation(evidence.file(b"AAAAAA"), evidence.file(b"BAAAAB"))
    evidence.operation(evidence.file(b"BAHAAB"), evidence.file(b"AAHAAA"), restore=True)
    evidence.path.write_bytes(b"AAHAAA")
    state = evidence.state()
    assert state["origin"] == "external"
    assert state["edits"][0]["after"] == "AAHAAA"
    assert all(edit["origin"] == "external" for edit in state["edits"])


def test_agent_returning_to_original_value_is_not_reported_as_a_remaining_change(tmp_path):
    evidence = Evidence(tmp_path)
    evidence.operation(evidence.file(b"AAAAAA"), evidence.file(b"BAAAAA"))
    evidence.operation(evidence.file(b"BAAAAA"), evidence.file(b"AAAAAA"))
    state = evidence.state()
    assert state["origin"] == "unchanged"
    assert state["edits"] == state["properties"] == []


def test_unobserved_content_is_not_classified_as_external(tmp_path):
    evidence = Evidence(tmp_path)
    original = b"A" * 131072
    edited = b"B" + original[1:]
    evidence.operation(evidence.file(original, complete=False), evidence.file(edited, complete=False))
    evidence.path.write_bytes(edited[:65536] + b"H" * 65536)
    state = evidence.state()
    assert state["origin"] == "agent"
    assert not state["content_complete"]
    assert any("未观察" in limitation for limitation in state["limitations"])


def test_external_append_is_shown_with_agent_results(tmp_path):
    evidence = Evidence(tmp_path)
    evidence.operation(evidence.file(b"AAAAAA"), evidence.file(b"BAAAAA"))
    evidence.path.write_bytes(b"BAAAAAH")
    state = evidence.state()
    assert state["origin"] == "mixed"
    assert state["edits"][0]["after"] == "BAAAAAH"
    assert state["content_complete"]


def test_metadata_only_evidence_does_not_claim_unobserved_file_bytes(tmp_path):
    evidence = Evidence(tmp_path)
    before = evidence.file(b"AAAAAA")
    before.update(blocks={}, complete=False)
    after = {**before, "mode": 0o444}
    evidence.operation(before, after)
    state = evidence.state()
    assert state["edits"] == []
    assert not state["content_complete"]


def test_replaced_file_reports_identity_change_even_with_identical_content(tmp_path):
    evidence = Evidence(tmp_path)
    evidence.operation(evidence.file(b"AAAAAA"), evidence.file(b"BAAAAA"))
    replacement = evidence.root / "replacement"
    replacement.write_bytes(b"BAAAAA")
    os.replace(replacement, evidence.path)
    state = evidence.state()
    assert {"origin": "external", "description": "文件对象被替换"} in state["properties"]
    assert state["origin"] == "mixed"


def test_later_agent_edit_does_not_erase_an_external_file_replacement(tmp_path):
    evidence = Evidence(tmp_path)
    evidence.operation(evidence.file(b"AAAAAA"), evidence.file(b"BAAAAA"))
    replacement = evidence.root / "replacement"
    replacement.write_bytes(b"BAAAAA")
    os.replace(replacement, evidence.path)
    with evidence.path.open("rb") as file:
        evidence.identity = native.identity(file)
    evidence.operation(evidence.file(b"BAAAAA"), evidence.file(b"BAAAAB"))
    evidence.path.write_bytes(b"BAAAAB")
    state = evidence.state()
    assert {"origin": "external", "description": "文件对象被替换"} in state["properties"]
    assert state["origin"] == "mixed"


def test_query_uses_only_published_receipts_and_never_updates_evidence(tmp_path):
    evidence = Evidence(tmp_path)
    evidence.operation(evidence.file(b"AAAAAA"), evidence.file(b"BAAAAA"))
    evidence.operation(evidence.file(b"BAAAAA"), evidence.file(b"BBBBBB"), published=False)
    evidence.path.write_bytes(b"BAAAAA")
    before = {p.relative_to(evidence.store): p.read_bytes() for p in evidence.store.rglob("*") if p.is_file()}
    with patch.object(Path, "rglob", side_effect=AssertionError("query scanned the tree")):
        assert evidence.state()["origin"] == "agent"
    assert {p.relative_to(evidence.store): p.read_bytes() for p in evidence.store.rglob("*") if p.is_file()} == before


def test_corrupt_evidence_is_not_downgraded_to_unknown(tmp_path):
    evidence = Evidence(tmp_path)
    before, after = evidence.file(b"AAAAAA"), evidence.file(b"BAAAAA")
    evidence.operation(before, after)
    (evidence.store / "cas" / after["blocks"]["0"]).write_bytes(b"broken")
    with pytest.raises(ValueError, match="CAS evidence is corrupt"):
        evidence.state()


def test_missing_published_receipt_is_a_corruption_error(tmp_path):
    evidence = Evidence(tmp_path)
    evidence.operation(evidence.file(b"AAAAAA"), evidence.file(b"BAAAAA"))
    (evidence.store / "commands" / evidence.active[0] / "sealed.json").unlink()
    with pytest.raises(ValueError, match="published operation evidence is missing"):
        evidence.state()


def test_state_query_does_not_wait_for_an_executing_operation(tmp_path):
    evidence = Evidence(tmp_path)
    evidence.operation(evidence.file(b"AAAAAA"), evidence.file(b"BAAAAA"))
    evidence.path.write_bytes(b"BAAAAA")
    view = WorkspaceFiles(evidence.root, evidence.store)
    async def scenario():
        async with view._owner():
            result = await asyncio.wait_for(view.changes(evidence.path), 5)
            assert result["changes"][0]["origin"] == "agent"
            assert result["content_complete"]
    asyncio.run(scenario())


def test_query_allows_another_reader_of_the_same_file(tmp_path):
    evidence = Evidence(tmp_path)
    evidence.operation(evidence.file(b"AAAAAA"), evidence.file(b"BAAAAA"))
    evidence.path.write_bytes(b"BAAAAA")
    with native.open_file(evidence.path, shared_read=True):
        assert evidence.state()["origin"] == "agent"


def test_query_rejects_a_file_changing_during_the_read(tmp_path):
    evidence = Evidence(tmp_path)
    evidence.operation(evidence.file(b"AAAAAA"), evidence.file(b"BAAAAA"))
    actual_stat = os.fstat
    calls = 0
    def changing(fd):
        nonlocal calls
        calls += 1
        info = actual_stat(fd)
        if calls >= 2:
            changed = list(info)
            changed[6] += 1
            return os.stat_result(changed)
        return info
    from redpanda.sandbox.files.changes import ChangesReadConflict
    with patch("redpanda.sandbox.files.changes.os.fstat", changing):
        with pytest.raises(ChangesReadConflict, match="查询期间"):
            evidence.state()


def test_adjacent_agent_and_external_line_edits_have_individual_origins(tmp_path):
    evidence = Evidence(tmp_path)
    before = b"a=1\nb=1\nc=1\n"
    evidence.operation(evidence.file(before), evidence.file(b"a=2\nb=1\nc=1\n"))
    evidence.path.write_bytes(b"a=2\nb=3\nc=1\n")
    state = evidence.state()
    assert [(e["origin"], e["before"], e["after"], e["byte_offset"]) for e in state["edits"]] == [
        ("agent", "a=1\n", "a=2\n", 0), ("external", "b=1\n", "b=3\n", 4),
    ]


def test_external_overwrite_does_not_leave_an_agent_result(tmp_path):
    evidence = Evidence(tmp_path)
    evidence.operation(evidence.file(b"AAAAAA"), evidence.file(b"BAAAAA"))
    evidence.path.write_bytes(b"HAAAAA")
    state = evidence.state()
    assert state["origin"] == "external"
    assert state["edits"][0]["origin"] == "external"


def test_unrelated_external_files_are_not_discovered_or_returned(tmp_path):
    evidence = Evidence(tmp_path)
    evidence.operation(evidence.file(b"AAAAAA"), evidence.file(b"BAAAAA"))
    evidence.path.write_bytes(b"BAAAAA")
    (evidence.root / "unrelated.txt").write_text("external")
    result = attribution(evidence.root, evidence.store, evidence.root)
    assert [item["path"] for item in result["changes"]] == ["file"]
    assert result["scope"] == "agent_touched_files"
    assert result["content_complete"]


def test_empty_agent_history_only_claims_no_agent_related_results(tmp_path):
    evidence = Evidence(tmp_path)
    result = attribution(evidence.root, evidence.store, evidence.root)
    assert result["changes"] == []
    assert result["scope"] == "agent_touched_files"


def test_new_file_and_deleted_file_have_real_before_and_after_content(tmp_path):
    evidence = Evidence(tmp_path)
    evidence.operation(image(), evidence.file(b"AAAAAA"))
    state = evidence.state()
    assert state["origin"] == "agent"
    assert (state["edits"][0]["before"], state["edits"][0]["after"]) == ("", "AAAAAA")
    evidence.operation(evidence.file(b"AAAAAA"), image())
    evidence.path.unlink()
    state = evidence.state()
    assert state["origin"] == "unchanged"
    assert state["edits"] == []


def test_binary_change_is_explicitly_missing_content(tmp_path):
    evidence = Evidence(tmp_path)
    evidence.operation(evidence.file(b"\0AAAAA"), evidence.file(b"\0BAAAA"))
    evidence.path.write_bytes(b"\0BAAAA")
    state = evidence.state()
    assert state["origin"] == "agent"
    assert state["edits"] == []
    assert not state["content_complete"]
    assert any("二进制" in reason for reason in state["limitations"])


def test_large_response_clearly_marks_omitted_content(tmp_path):
    evidence = Evidence(tmp_path)
    evidence.operation(evidence.file(b"AAAAAA"), evidence.file(b"BAAAAA"))
    evidence.path.write_bytes(b"BAAAAA")
    with patch("redpanda.sandbox.files.changes.MAX_CONTENT_CHARS", 5):
        result = attribution(evidence.root, evidence.store, evidence.root)
    edit = result["changes"][0]["edits"][0]
    assert len(edit["before"]) + len(edit["after"]) <= 5
    assert edit["truncated"] and result["truncated"]
    assert not result["content_complete"]


def test_utf8_offsets_are_current_file_byte_positions(tmp_path):
    evidence = Evidence(tmp_path)
    before, after = "你好\na=1\n".encode(), "你好\na=2\n".encode()
    evidence.operation(evidence.file(before), evidence.file(after))
    evidence.path.write_bytes(after)
    edit = evidence.state()["edits"][0]
    assert edit["byte_offset"] == len("你好\n".encode())
    assert edit["after"] == "a=2\n"


@pytest.mark.parametrize("old,new", [("甲" * 40, "乙" * 53), ("A" * 80, "B" * 80)])
def test_single_file_difference_can_be_read_completely_across_pages(tmp_path, old, new):
    evidence = Evidence(tmp_path)
    evidence.operation(evidence.file(old.encode()), evidence.file(new.encode()))
    evidence.path.write_bytes(new.encode())
    before, after, offset = "", "", 0
    with patch("redpanda.sandbox.files.changes.MAX_CONTENT_CHARS", 13):
        while True:
            result = attribution(evidence.root, evidence.store, evidence.path, text_offset=offset)
            file = result["changes"][0]
            for edit in file["edits"]:
                before += edit["before"]
                after += edit["after"]
            following = file["next_text_offset"]
            if following is None:
                break
            assert following > offset
            offset = following
    assert before == old and after == new


def test_directory_query_can_continue_past_the_file_limit(tmp_path):
    evidence = Evidence(tmp_path)
    evidence.operation(evidence.file(b"AAAAAA"), evidence.file(b"BAAAAA"))
    evidence.path.write_bytes(b"BAAAAA")
    os.link(evidence.path, evidence.root / "other")
    receipt_path = evidence.store / "commands" / evidence.active[0] / "sealed.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["changes"].append({**receipt["changes"][0], "path": "/other"})
    receipt_path.write_text(json.dumps(receipt))
    with patch("redpanda.sandbox.files.changes.MAX_FILES", 1):
        first = attribution(evidence.root, evidence.store, evidence.root)
        second = attribution(evidence.root, evidence.store, evidence.root, offset=first["next_offset"])
    assert first["next_offset"] == 1 and second["next_offset"] is None
    assert [item["path"] for page in (first, second) for item in page["changes"]] == ["file", "other"]
