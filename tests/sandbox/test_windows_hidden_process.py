"""Hidden metadata follows the same evidence, publication and restoration contract."""
import os
from pathlib import Path
import stat

import pytest

from redpanda.filesystem.changes import attribution
from redpanda.filesystem.file_view.client import native_executable
from redpanda.filesystem.file_view import native, publication as pub
from tests.fixtures.file_view import CommandClient

pytestmark = [pytest.mark.process, pytest.mark.skipif(os.name != "nt", reason="Windows attributes")]


def is_hidden(path):
    return bool(path.stat().st_file_attributes & stat.FILE_ATTRIBUTE_HIDDEN)


def set_hidden(path, value):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    kernel.GetFileAttributesW.restype = wintypes.DWORD
    kernel.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    kernel.SetFileAttributesW.restype = wintypes.BOOL
    attributes = kernel.GetFileAttributesW(str(path)) & ~stat.FILE_ATTRIBUTE_NORMAL
    attributes = attributes | 2 if value else attributes & ~2
    if not kernel.SetFileAttributesW(str(path), attributes or stat.FILE_ATTRIBUTE_NORMAL):
        raise ctypes.WinError(ctypes.get_last_error())


def fixture(tmp_path):
    base = tmp_path / "project"
    base.mkdir()
    (base / "file").write_bytes(b"unobserved content" * 10000)
    (base / "directory").mkdir()
    return base, tmp_path / "store"


def test_hidden_metadata_roundtrips_without_content_evidence(tmp_path):
    base, store = fixture(tmp_path)
    set_hidden(base / "directory", True)
    identities = {name: native.directory_identity(base / name) for name in ("file", "directory")}
    with CommandClient(native_executable(), store, base) as client:
        mount = Path(client.begin("attributes")["mount"])
        assert is_hidden(mount / "directory")
        set_hidden(mount / "file", True)
        set_hidden(mount / "directory", False)
        (mount / ".plain").write_text("ordinary dot name")
        assert not is_hidden(mount / ".plain")
        assert not is_hidden(base / "file")
        assert is_hidden(base / "directory")
        receipt = client.request("finish")["receipt"]
        changes = {item["path"]: item for item in receipt["changes"]}
        for name in ("file", "directory"):
            change = changes["/" + name]
            assert change["before"]["hidden"] != change["after"]["hidden"]
            assert change["before"]["blocks"] == change["after"]["blocks"] == {}
        assert not changes["/file"]["before"]["complete"]
        client.request("accept", command_id="attributes")
        assert pub.publish(client)["state"] == "complete"
        assert is_hidden(base / "file")
        assert not is_hidden(base / "directory")
        report = attribution(base, store, base)
        assert any("隐藏属性" in prop["description"] for item in report["changes"] for prop in item["properties"])
        assert client.restore_published(["attributes"], "original")["state"] == "complete"
        assert not is_hidden(base / "file")
        assert is_hidden(base / "directory")
    for name, identity in identities.items():
        assert native.directory_identity(base / name) == identity
    assert (base / "file").read_bytes() == b"unobserved content" * 10000


@pytest.mark.parametrize("policy", ["original", "preserve"])
def test_view_restore_can_restore_a_hidden_attribute_and_the_restore(tmp_path, policy):
    base, store = fixture(tmp_path)
    with CommandClient(native_executable(), store, base) as client:
        mount = Path(client.begin("hide")["mount"])
        set_hidden(mount / "file", True)
        set_hidden(mount / "directory", True)
        client.request("finish")
        client.request("accept", command_id="hide")
        result = client.restore("undo", ["hide"], policy)
        assert result["status"] == "sealed", result
        assert all(not change["after"]["hidden"] for change in result["receipt"]["changes"])
        client.request("accept", command_id="undo")
        result = client.restore("redo", ["undo"], "original")
        assert result["status"] == "sealed", result
        assert all(change["after"]["hidden"] for change in result["receipt"]["changes"])
        client.request("accept", command_id="redo")
        assert pub.publish(client)["state"] == "complete"
    assert is_hidden(base / "file") and is_hidden(base / "directory")


def test_hidden_survives_copyup_rename_and_reopening_the_view(tmp_path):
    base, store = fixture(tmp_path)
    set_hidden(base / "file", True)
    set_hidden(base / "directory", True)
    with CommandClient(native_executable(), store, base) as client:
        mount = Path(client.begin("rename")["mount"])
        with (mount / "file").open("r+b") as file:
            file.write(b"edited")
        (mount / "file").rename(mount / "renamed")
        (mount / "directory/child").write_text("child")
        assert is_hidden(mount / "renamed") and is_hidden(mount / "directory")
        client.request("finish")
        client.request("accept", command_id="rename")
    with CommandClient(native_executable(), store, base) as client:
        mount = Path(client.begin("probe")["mount"])
        assert is_hidden(mount / "renamed") and is_hidden(mount / "directory")
        client.request("finish")
        client.request("discard", command_id="probe")
        assert pub.publish(client)["state"] == "complete"
    assert is_hidden(base / "renamed") and is_hidden(base / "directory")


def test_attribute_only_publication_preserves_readonly_and_archive_bits(tmp_path):
    base, store = fixture(tmp_path)
    path = base / "file"
    path.chmod(stat.S_IREAD)
    before = path.stat().st_file_attributes
    try:
        with CommandClient(native_executable(), store, base) as client:
            mount = Path(client.begin("hide")["mount"])
            set_hidden(mount / "file", True)
            client.request("finish")
            client.request("accept", command_id="hide")
            assert pub.publish(client)["state"] == "complete"
            assert path.stat().st_file_attributes == before | stat.FILE_ATTRIBUTE_HIDDEN
            assert client.restore_published(["hide"], "original")["state"] == "complete"
            assert path.stat().st_file_attributes == before
    finally:
        path.chmod(stat.S_IWRITE)


def test_content_publication_and_undo_preserve_unrelated_host_hidden_attribute(tmp_path):
    base, store = fixture(tmp_path)
    with CommandClient(native_executable(), store, base) as client:
        mount = Path(client.begin("content")["mount"])
        with (mount / "file").open("r+b") as file:
            file.write(b"EDIT")
        client.request("finish")
        client.request("accept", command_id="content")
        set_hidden(base / "file", True)
        assert pub.publish(client)["state"] == "complete"
        assert is_hidden(base / "file")
        assert client.restore_published(["content"], "original")["state"] == "complete"
        assert is_hidden(base / "file")
        assert (base / "file").read_bytes() == b"unobserved content" * 10000


def test_creation_can_request_hidden_without_deriving_it_from_name(tmp_path):
    from redpanda.filesystem.file_view import native_win as win
    base, store = fixture(tmp_path)
    with CommandClient(native_executable(), store, base) as client:
        mount = Path(client.begin("create")["mount"])
        handle = win.create_file(str(mount / "born"), 0x40000000, 0, None, 1, 2, None)
        assert handle != win.INVALID
        win.checked(win.close_handle(handle))
        assert is_hidden(mount / "born")
        client.request("finish")
        client.request("accept", command_id="create")
        assert pub.publish(client)["state"] == "complete"
    assert is_hidden(base / "born")


@pytest.mark.parametrize("policy", ["original", "preserve"])
def test_attribute_restore_keeps_later_unobserved_content(tmp_path, policy):
    base, store = fixture(tmp_path)
    with CommandClient(native_executable(), store, base) as client:
        mount = Path(client.begin("hide")["mount"])
        set_hidden(mount / "file", True)
        client.request("finish")
        client.request("accept", command_id="hide")
        assert pub.publish(client)["state"] == "complete"
        with (base / "file").open("ab") as file:
            file.write(b"USER APPEND")
        result = client.restore("undo", ["hide"], policy)
        assert result["status"] == "sealed", result
        client.request("accept", command_id="undo")
        assert pub.publish(client)["state"] == "complete"
    assert not is_hidden(base / "file")
    assert (base / "file").read_bytes() == b"unobserved content" * 10000 + b"USER APPEND"


def test_interrupted_hidden_directory_publication_resumes_owned_object(tmp_path, monkeypatch):
    base, store = fixture(tmp_path)
    with CommandClient(native_executable(), store, base) as client:
        mount = Path(client.begin("create")["mount"])
        (mount / "new").mkdir()
        set_hidden(mount / "new", True)
        client.request("finish")
        client.request("accept", command_id="create")
        original = native.set_path_attributes
        def interrupted(*args):
            raise RuntimeError("interrupted before attributes")
        monkeypatch.setattr(native, "set_path_attributes", interrupted)
        with pytest.raises(RuntimeError, match="interrupted before attributes"):
            pub.publish(client)
        identity = native.directory_identity(base / "new")
        monkeypatch.setattr(native, "set_path_attributes", original)
        pending = pub.unfinished(client.store)
        assert len(pending) == 1
        assert pub.execute(client, pending[0])["state"] == "complete"
        assert native.directory_identity(base / "new") == identity
        assert is_hidden(base / "new")
