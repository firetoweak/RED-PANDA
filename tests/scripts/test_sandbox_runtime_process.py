"""真实运行已打包的程序：没有 Rust/LLVM 的 PATH 仍可挂载和修改文件。"""
import os
from pathlib import Path
import shutil

import pytest

from scripts import sandbox_runtime as runtime
from tests.fixtures.file_view import CommandClient

pytestmark = pytest.mark.process


def test_packaged_runtime_mounts_without_developer_toolchains(tmp_path, monkeypatch):
    target = runtime.host_platform()
    archive = runtime.ROOT / f"dist/sandbox/redpanda-sandbox-{target}.zip"
    if not archive.exists():
        pytest.skip("需要先构建并打包沙箱运行程序")
    config = tmp_path / "native/sandbox/release.json"
    config.parent.mkdir(parents=True)
    shutil.copyfile(runtime.ROOT / "native/sandbox/release.json", config)
    monkeypatch.delenv("REDPANDA_FILESYSTEM_EXECUTABLE", raising=False)
    monkeypatch.setattr(runtime, "download", lambda url, destination:
                        shutil.copyfile(archive.with_name(url.rsplit("/", 1)[1]), destination))
    executable = runtime.install(tmp_path)
    environment = dict(os.environ)
    if os.name == "nt":
        environment["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
    else:
        helpers = tmp_path / "runtime-helpers"
        helpers.mkdir()
        for command in ("fusermount3", "fusermount"):
            source = shutil.which(command)
            if source:
                (helpers / command).symlink_to(source)
        environment["PATH"] = str(helpers)
    for command in ("rustup", "cargo", "rustc", "clang"):
        assert shutil.which(command, path=environment["PATH"]) is None
    base = tmp_path / "workspace"
    base.mkdir()
    (base / "file.txt").write_text("before", encoding="utf-8")
    with CommandClient(executable, tmp_path / "store", base, service_environment=environment) as client:
        begin = client.begin("edit")
        assert begin["status"] == "active"
        (Path(begin["mount"]) / "file.txt").write_text("after", encoding="utf-8")
        assert client.request("finish")["status"] == "sealed"
        assert client.request("accept", command_id="edit")["status"] == "accepted"
        probe = client.begin("read")
        assert (Path(probe["mount"]) / "file.txt").read_text(encoding="utf-8") == "after"
        client.request("finish")
        client.request("discard", command_id="read")
    assert (base / "file.txt").read_text(encoding="utf-8") == "before"
