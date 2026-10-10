import json
import sqlite3
import tracemalloc
import unittest
from unittest.mock import patch

import pytest

from redpanda.assistant.artifacts import (
    ARTIFACT_BLOCK_CHARS, FileArtifactStore, MemoryArtifactGateway, MemoryArtifactStore,
)
from redpanda.assistant.content import read_content_result
from redpanda.assistant.context.projection import ModelContextProjector, externalize_payload
from redpanda.runtime import AgentRuntime, InvokeTool, MemoryJournal, ModelDecision, StateProjector, ToolBinding, CommandOutcome, OutcomeStatus
from tests.session_scheduler import settle_session


@pytest.mark.parametrize("disk", [False, True])
def test_search_returns_cross_block_literal_matches_with_a_shared_body_budget(tmp_path, disk):
    store = FileArtifactStore(tmp_path) if disk else MemoryArtifactStore()
    query = "超时😀[x].*"
    text = "a" * (ARTIFACT_BLOCK_CHARS - 3) + query + "b" * 15000 + query + "c" * 2000
    ref = store.save(text).artifact_id
    first = read_content_result(store, {"reference": ref, "query": query, "limit": 1500})
    data = first["data"]
    assert len(data["fragments"]) == 2
    assert sum(len(f["content"]) for f in data["fragments"]) <= 1500
    for fragment in data["fragments"]:
        assert fragment["content"] == text[fragment["offset"]:fragment["end_offset"]]
        assert query in fragment["content"]
    assert data["next_offset"] is None
    assert not read_content_result(store, {"reference": ref, "query": "不存在"})["data"]["fragments"]


@pytest.mark.parametrize("disk", [False, True])
def test_search_continuation_and_expansion_use_original_character_coordinates(tmp_path, disk):
    store = FileArtifactStore(tmp_path) if disk else MemoryArtifactStore()
    text = ("前文" * 1000 + "needle" + "后文" * 1000) * 4
    ref = store.save(text).artifact_id
    matches = []
    offset = 0
    while True:
        data = read_content_result(store, {"reference": ref, "query": "needle", "offset": offset, "limit": 1000})["data"]
        assert sum(len(f["content"]) for f in data["fragments"]) <= 1000
        matches.extend(f["match_offset"] for f in data["fragments"])
        if data["next_offset"] is None:
            break
        assert data["next_offset"] > offset
        offset = data["next_offset"]
    assert matches == [i for i in range(len(text)) if text.startswith("needle", i)]
    expanded = read_content_result(store, {"reference": ref, "offset": matches[1] - 500, "limit": 2000})["data"]["fragments"][0]
    assert expanded["content"] == text[matches[1] - 500:matches[1] + 1500]


def test_small_disk_read_has_bounded_python_memory(tmp_path):
    store = FileArtifactStore(tmp_path)
    text = "汉😀abc" * 600000
    ref = store.save(text).artifact_id
    tracemalloc.start()
    try:
        chunk = store.read(ref, len(text) - 20000, 12000)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert chunk.content == text[-20000:-8000]
    assert peak < 512 * 1024


def test_corrupt_artifact_is_not_reported_as_no_match(tmp_path):
    store = FileArtifactStore(tmp_path)
    ref = store.save("x" * 20000).artifact_id
    with sqlite3.connect(tmp_path / "contents.sqlite") as db:
        db.execute("DELETE FROM blocks WHERE artifact_id=? AND block_index=1", (ref,))
    with pytest.raises(ValueError, match="incomplete"):
        read_content_result(store, {"reference": ref, "query": "missing"})


@pytest.mark.parametrize("query", ["", "😀"])
def test_largest_unicode_page_fits_runtime_and_preserves_original_hint(query):
    gateway = MemoryArtifactGateway()
    store = gateway.for_session("s")
    payload = {"ok": True, "code": "OK", "data": "😀" * 40000, "error": None, "hint": "请核对来源。"}
    stub, ref = externalize_payload(payload, store, max_chars=32000, preview_chars=2000)
    assert "请核对来源。" in stub["hint"] and "read_content" in stub["hint"]
    result = read_content_result(store, {"reference": ref, "limit": 32000, "query": query})
    CommandOutcome(OutcomeStatus.SUCCEEDED, value=result)


class ReaderProjectionTest(unittest.IsolatedAsyncioTestCase):
    async def test_dehydration_keeps_original_reference_without_another_artifact(self):
        gateway = MemoryArtifactGateway()
        store = gateway.for_session("s")
        ref = store.save("needle " + "x" * 40000).artifact_id
        decisions = [ModelDecision(command_requests=(InvokeTool("read_content", (("reference", ref), ("limit", 32000))),)), ModelDecision(content="已读")]
        class Decision:
            async def decide(self, frame):
                return decisions.pop(0)
        async def read(context, arguments):
            return read_content_result(store, arguments)
        runtime = AgentRuntime(MemoryJournal(), Decision(), {"read_content": ToolBinding(read)})
        await runtime.receive_user_message("s", "读取", delivery_id="first")
        await settle_session(runtime, "s")
        projector = ModelContextProjector(gateway=gateway)
        events = await runtime.snapshot("s")
        prepared = projector.prepare(events, StateProjector().project_visible("s", events), "s")
        tool = json.loads(next(m["content"] for m in prepared.messages if m["role"] == "tool"))
        assert len(tool["data"]["fragments"][0]["content"]) == 32000
        await runtime.receive_user_message("s", "继续", delivery_id="next")
        events = await runtime.snapshot("s")
        with patch.object(store, "save", side_effect=AssertionError("reader must not create a nested artifact")):
            prepared = projector.prepare(events, StateProjector().project_visible("s", events), "s")
        tool = json.loads(next(m["content"] for m in prepared.messages if m["role"] == "tool"))
        assert tool["data"]["reference"] == ref
        assert tool["data"]["fragments"] == [{"offset": 0, "end_offset": 32000}]
