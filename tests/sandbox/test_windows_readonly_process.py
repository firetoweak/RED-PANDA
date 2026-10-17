"""Every accepted read-only attribute effect has publication and restoration evidence."""
import os
from pathlib import Path

import pytest

from redpanda.filesystem.changes import attribution
from redpanda.filesystem.file_view import native, publication as pub
from redpanda.filesystem.file_view.client import native_executable
from tests.fixtures.file_view import CommandClient
from tests.fixtures.windows_attributes import flags, set_flag

pytestmark = [pytest.mark.process, pytest.mark.skipif(os.name != "nt", reason="Windows attributes")]


def fixture(tmp_path):
    base = tmp_path / "project"; base.mkdir()
    (base / "file").write_bytes(b"original content")
    (base / "directory").mkdir()
    return base, tmp_path / "store"


@pytest.mark.parametrize("name", ["file", "directory"])
def test_readonly_is_attribute_only_evidence_and_host_undo(tmp_path, name):
    base, store = fixture(tmp_path); path = base / name
    set_flag(path, 2, True)
    identity = native.directory_identity(path)
    with CommandClient(native_executable(), store, base) as client:
        mount = Path(client.begin("readonly")["mount"])
        set_flag(mount / name, 1, True)
        receipt = client.request("finish")["receipt"]
        change, = receipt["changes"]
        assert change["before"]["readonly"] is False and change["after"]["readonly"] is True
        assert change["before"]["hidden"] is change["after"]["hidden"] is True
        assert change["before"]["blocks"] == change["after"]["blocks"] == {}
        assert not flags(path) & 1
        client.request("accept", command_id="readonly")
        assert pub.publish(client)["state"] == "complete"
        assert flags(path) & 3 == 3
        probe = Path(client.begin("probe")["mount"])
        assert flags(probe / name) & 3 == 3
        client.request("finish"); client.request("discard", command_id="probe")
        report = attribution(base, store, base)
        assert any("只读属性" in p["description"] for f in report["changes"] for p in f["properties"])
        assert pub.undo(client, "readonly", "original")["state"] == "complete"
        assert flags(path) & 3 == 2
    assert native.directory_identity(path) == identity


def test_child_creation_preserves_inherited_readonly_directory(tmp_path):
    base, store = fixture(tmp_path); path = base / "directory"
    set_flag(path, 1, True)
    try:
        with CommandClient(native_executable(), store, base) as client:
            mount = Path(client.begin("child")["mount"])
            assert flags(mount / "directory") & 1
            (mount / "directory/child").write_bytes(b"child")
            assert flags(mount / "directory") & 1
            changes = client.request("finish")["receipt"]["changes"]
            assert {value["path"] for value in changes} == {"/directory/child"}
            client.request("accept", command_id="child")
            assert pub.publish(client)["state"] == "complete"
            assert flags(path) & 1 and (path / "child").read_bytes() == b"child"
    finally:
        set_flag(path, 1, False)


@pytest.mark.parametrize("route", ["host", "view"])
@pytest.mark.parametrize("policy", ["original", "preserve"])
def test_readonly_restore_keeps_later_content_and_can_itself_be_undone(tmp_path, route, policy):
    base, store = fixture(tmp_path); path = base / "file"
    try:
        with CommandClient(native_executable(), store, base) as client:
            mount = Path(client.begin("readonly")["mount"])
            set_flag(mount / "file", 1, True)
            client.request("finish"); client.request("accept", command_id="readonly")
            assert pub.publish(client)["state"] == "complete"
            set_flag(path, 1, False)
            with path.open("ab") as file: file.write(b" USER APPEND")
            set_flag(path, 1, True)
            if route == "host":
                assert pub.undo(client, "readonly", policy)["state"] == "complete"
            else:
                result = client.restore("undo", ["readonly"], policy)
                assert result["status"] == "sealed", result
                client.request("accept", command_id="undo")
                assert pub.publish(client)["state"] == "complete"
                result = client.restore("redo", ["undo"], "original")
                assert result["status"] == "sealed", result
                client.request("accept", command_id="redo")
                assert pub.publish(client)["state"] == "complete"
            assert bool(flags(path) & 1) == (route == "view")
            assert path.read_bytes() == b"original content USER APPEND"
    finally:
        if path.exists(): set_flag(path, 1, False)


def test_content_changes_to_an_existing_readonly_file_roundtrip(tmp_path):
    base, store = fixture(tmp_path); path = base / "file"
    set_flag(path, 2, True); set_flag(path, 1, True)
    identity = native.directory_identity(path)
    try:
        with CommandClient(native_executable(), store, base) as client:
            mount = Path(client.begin("content")["mount"])
            set_flag(mount / "file", 1, False)
            with (mount / "file").open("r+b") as file:
                file.write(b"edited"); file.truncate()
            set_flag(mount / "file", 1, True)
            client.request("finish"); client.request("accept", command_id="content")
            assert pub.publish(client)["state"] == "complete"
            assert path.read_bytes() == b"edited" and flags(path) & 3 == 3
            assert pub.undo(client, "content", "original")["state"] == "complete"
            assert path.read_bytes() == b"original content" and flags(path) & 3 == 3
            assert native.directory_identity(path) == identity
    finally:
        set_flag(path, 1, False)


def test_readonly_file_deletion_and_restoration(tmp_path):
    base, store = fixture(tmp_path); path = base / "file"
    set_flag(path, 1, True)
    try:
        with CommandClient(native_executable(), store, base) as client:
            mount = Path(client.begin("delete")["mount"])
            set_flag(mount / "file", 1, False); (mount / "file").unlink()
            client.request("finish"); client.request("accept", command_id="delete")
            assert pub.publish(client)["state"] == "complete"
            assert not path.exists()
            assert pub.undo(client, "delete", "original")["state"] == "complete"
            assert path.read_bytes() == b"original content" and flags(path) & 1
    finally:
        if path.exists(): set_flag(path, 1, False)


def test_publication_resumes_after_releasing_readonly_for_content_write(tmp_path, monkeypatch):
    base, store = fixture(tmp_path); path = base / "file"
    set_flag(path, 1, True)
    try:
        with CommandClient(native_executable(), store, base) as client:
            mount = Path(client.begin("content")["mount"])
            set_flag(mount / "file", 1, False); (mount / "file").write_bytes(b"edited")
            set_flag(mount / "file", 1, True)
            client.request("finish"); client.request("accept", command_id="content")
            original = native.open_file
            def interrupted(*args, **kwargs):
                if kwargs.get("write"): raise RuntimeError("interrupted before bytes")
                return original(*args, **kwargs)
            monkeypatch.setattr(native, "open_file", interrupted)
            with pytest.raises(RuntimeError, match="interrupted before bytes"): pub.publish(client)
            assert not flags(path) & 1 and path.read_bytes() == b"original content"
            monkeypatch.setattr(native, "open_file", original)
            pending, = pub.unfinished(client.store)
            assert pub.execute(client, pending)["state"] == "complete"
            assert path.read_bytes() == b"edited" and flags(path) & 1
    finally:
        set_flag(path, 1, False)


@pytest.mark.parametrize("disposition", [1, 2])
def test_initial_and_overwrite_attributes_are_real_effects(tmp_path, disposition):
    from redpanda.filesystem.file_view import native_win as win
    base, store = fixture(tmp_path); name = "new" if disposition == 1 else "file"
    try:
        with CommandClient(native_executable(), store, base) as client:
            mount = Path(client.begin("attributes")["mount"])
            handle = win.create_file(str(mount / name), 0x40000000, 0, None, disposition, 3, None)
            assert handle != win.INVALID
            win.checked(win.close_handle(handle))
            assert flags(mount / name) & 3 == 3
            client.request("finish"); client.request("accept", command_id="attributes")
            assert pub.publish(client)["state"] == "complete"
            assert flags(base / name) & 3 == 3 and (base / name).read_bytes() == b""
    finally:
        if (base / name).exists(): set_flag(base / name, 1, False)


@pytest.mark.parametrize("disposition", [1, 2])
def test_unsupported_creation_attributes_refuse_before_creation_or_truncation(tmp_path, disposition):
    from redpanda.filesystem.file_view import native_win as win
    base, store = fixture(tmp_path); name = "new" if disposition == 1 else "file"
    with CommandClient(native_executable(), store, base) as client:
        mount = Path(client.begin("unsupported")["mount"])
        handle = win.create_file(str(mount / name), 0x40000000, 0, None, disposition, 4, None)
        assert handle == win.INVALID
        assert win.c.get_last_error() == 50
        if disposition == 1: assert not (mount / name).exists()
        else: assert (mount / name).read_bytes() == b"original content"
        assert client.request("finish")["receipt"]["changes"] == []


@pytest.mark.parametrize("operation", ["acl", "stream"])
def test_unsupported_windows_metadata_requests_leave_file_and_evidence_unchanged(tmp_path, operation):
    import ctypes
    from ctypes import wintypes
    from redpanda.filesystem.file_view import native_win as win
    base, store = fixture(tmp_path)
    with CommandClient(native_executable(), store, base) as client:
        mount = Path(client.begin("unsupported")["mount"])
        if operation == "stream":
            handle = win.create_file(str(mount / "file:stream"), 0x40000000, 0, None, 1, 0x80, None)
            assert handle == win.INVALID
        else:
            adv = ctypes.WinDLL("advapi32", use_last_error=True)
            convert = adv.ConvertStringSecurityDescriptorToSecurityDescriptorW
            convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
            convert.restype = wintypes.BOOL
            apply = adv.SetFileSecurityW
            apply.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
            apply.restype = wintypes.BOOL
            descriptor = ctypes.c_void_p()
            assert convert("D:P(A;;FR;;;WD)", 1, ctypes.byref(descriptor), None)
            try:
                assert not apply(str(mount / "file"), 4, descriptor)
            finally:
                free = win.api("LocalFree", [ctypes.c_void_p], ctypes.c_void_p)
                assert free(descriptor) is None
        assert (mount / "file").read_bytes() == b"original content"
        assert client.request("finish")["receipt"]["changes"] == []
