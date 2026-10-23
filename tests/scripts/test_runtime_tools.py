import hashlib
import io
import os
from pathlib import Path
import shutil
import zipfile

import pytest

from scripts import runtime_tools as tools


def package(data, sha256=None):
    return {"version": "1", "url": "https://example.test/tool.zip", "root": "tool",
            "bin": ".", "executable": "tool.exe", "sha256": sha256 or hashlib.sha256(data).hexdigest()}


def archive():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as bundle:
        bundle.writestr("tool/tool.exe", b"binary")
        bundle.writestr("tool/LICENSE", b"license")
    return stream.getvalue()


@pytest.mark.parametrize("valid", [True, False])
def test_download_is_verified_before_installation(tmp_path, monkeypatch, valid):
    data = archive()
    monkeypatch.setattr(tools, "urlopen", lambda *args, **kwargs: io.BytesIO(data))
    record = package(data, None if valid else "0" * 64)
    destination = tools.tool_directory(tmp_path, "tool")
    if valid:
        tools.install_package(tmp_path, "tool", record)
        assert (destination / "tool.exe").read_bytes() == b"binary"
        assert (destination / "LICENSE").read_bytes() == b"license"
    else:
        with pytest.raises(ValueError, match="校验失败"):
            tools.install_package(tmp_path, "tool", record)
        assert not destination.exists()


def test_local_tools_are_visible_only_in_the_process_environment(tmp_path, monkeypatch):
    record = package(b"")
    monkeypatch.setattr(tools, "packages", lambda root: {"tool": record})
    directory = tools.tool_directory(tmp_path, "tool")
    directory.mkdir(parents=True)
    monkeypatch.setenv("PATH", str(tmp_path / "system"))
    tools.activate(tmp_path)
    tools.activate(tmp_path)
    paths = os.environ["PATH"].split(os.pathsep)
    assert Path(paths[0]).resolve() == directory.resolve()
    assert paths[1:] == [str(tmp_path / "system")]


def test_windows_directory_move_can_use_the_standard_copy_path(tmp_path, monkeypatch):
    data = archive()
    monkeypatch.setattr(tools, "urlopen", lambda *args, **kwargs: io.BytesIO(data))
    def locked_directory(*args, **kwargs):
        raise PermissionError("directory rename denied")
    monkeypatch.setattr(os, "rename", locked_directory)
    tools.install_package(tmp_path, "tool", package(data))
    assert (tools.tool_directory(tmp_path, "tool") / "tool.exe").read_bytes() == b"binary"


def test_zip_helper_programs_keep_executable_permissions(tmp_path, monkeypatch):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as bundle:
        bundle.writestr("tool/tool.exe", b"binary")
        helper = zipfile.ZipInfo("tool/crash-handler")
        helper.external_attr = 0o100755 << 16
        bundle.writestr(helper, b"helper")
    data = stream.getvalue()
    monkeypatch.setattr(tools, "urlopen", lambda *args, **kwargs: io.BytesIO(data))
    chmods = []
    monkeypatch.setattr(Path, "chmod", lambda path, mode: chmods.append((path.name, mode)))
    tools.install_package(tmp_path, "tool", package(data))
    assert ("crash-handler", 0o755) in chmods


def test_existing_system_tools_do_not_trigger_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "packages", lambda root: {name: package(b"") for name in ("node", "fd", "rg", "git")})
    monkeypatch.setattr(tools, "supported_node", lambda: True)
    monkeypatch.setattr(shutil, "which", lambda name: f"system/{name}")
    monkeypatch.setattr(tools, "install_package", lambda *args: pytest.fail("must reuse system tools"))
    tools.prepare(tmp_path)


def test_browser_uses_application_install_even_when_system_chrome_exists(tmp_path, monkeypatch):
    record = package(b"")
    monkeypatch.setattr(tools, "packages", lambda root: {"chrome": record})
    monkeypatch.setattr(shutil, "which", lambda name: f"system/{name}")
    install = []
    monkeypatch.setattr(tools, "install_package", lambda root, name, package: install.append(name))
    tools.prepare(tmp_path)
    assert install == ["chrome"]
    browser = tools.tool_directory(tmp_path, "chrome") / record["executable"]
    browser.parent.mkdir(parents=True)
    browser.touch()
    tools.prepare(tmp_path)
    assert install == ["chrome"]


def test_missing_tools_are_prepared_before_setup_preflight(monkeypatch):
    from scripts import setup
    events = []
    monkeypatch.setattr(tools, "prepare", lambda root: events.append("tools"))
    monkeypatch.setattr(setup.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(setup, "check_dependencies", lambda: events.append("check") or ["still missing"])
    with pytest.raises(SystemExit):
        setup.main([])
    assert events == ["tools", "check"]


def test_linux_system_packages_use_the_available_package_manager(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: name if name == "apt-get" else None)
    commands = []
    monkeypatch.setattr(tools.subprocess, "run", lambda command, **kwargs: commands.append(command))
    tools.install_linux_packages(["git"])
    assert commands == [["sudo", "apt-get", "update"], ["sudo", "apt-get", "install", "-y", "git"]]
