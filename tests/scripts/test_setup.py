import subprocess
from types import SimpleNamespace

import pytest

from scripts import setup


def test_preflight_collects_missing_project_tools_and_node_version(monkeypatch):
    monkeypatch.setattr(setup.shutil, "which", lambda command: "node" if command == "node" else None)
    monkeypatch.setattr(setup.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="v20.0.0\n"))
    message = "\n".join(setup.check_dependencies())
    for dependency in ("Git", "ripgrep", "fd", "npm", "Node.js 版本"):
        assert dependency in message


def test_failed_preflight_never_starts_installation(monkeypatch):
    monkeypatch.setattr(setup, "check_dependencies", lambda: ["missing Git"])
    monkeypatch.setattr(setup.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not install"))
    with pytest.raises(SystemExit) as error:
        setup.main([])
    assert error.value.code == 1


@pytest.mark.parametrize("arguments,configure", [([], True), (["--no-configure"], False)])
def test_preparation_uses_project_environment_and_configures_only_when_requested(monkeypatch, arguments, configure):
    monkeypatch.setattr(setup, "check_dependencies", lambda: [])
    monkeypatch.setattr(setup.shutil, "which", lambda _: "npm")
    commands = []
    monkeypatch.setattr(setup.subprocess, "run", lambda command, **kwargs: commands.append((command, kwargs)))
    assert setup.main(arguments) == 0
    assert len(commands) == 5
    assert commands[0][0][1:] == ["pip", "install", "--python", setup.sys.executable, "pip"]
    assert commands[1][0][1:] == ["pip", "install", "--python", setup.sys.executable,
                                  "-r", "requirements.lock", "--require-hashes"]
    assert commands[2][0] == ["npm", "--prefix", "web", "ci"]
    assert commands[3][0] == [setup.sys.executable, "-m", "scripts.sandbox_runtime", "install", "--with-runtime"]
    assert commands[4][0] == [setup.sys.executable, "-m", "redpanda.initialize"] + (["--if-needed"] if configure else [])
    assert all(kwargs == {"cwd": setup.ROOT, "check": True} for _, kwargs in commands)


def test_project_preparation_does_not_invoke_rust_or_llvm(monkeypatch):
    project_tools = {"git", "rg", "fd", "node", "npm", "npm.cmd"}
    monkeypatch.setattr(setup.shutil, "which", lambda command: command if command in project_tools else None)
    commands = []

    def run(command, **kwargs):
        if command == ["node", "--version"]:
            return SimpleNamespace(stdout="v24.0.0\n")
        commands.append(command)

    monkeypatch.setattr(setup.subprocess, "run", run)
    assert setup.main(["--no-configure"]) == 0
    assert len(commands) == 5
    assert all("scripts/build_sandbox.py" not in command for command in commands)


def test_dependency_install_failure_stops_preparation(monkeypatch):
    monkeypatch.setattr(setup, "check_dependencies", lambda: [])
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(setup.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        setup.main([])
    assert len(calls) == 1


def test_check_mode_only_validates_dependencies(monkeypatch):
    monkeypatch.setattr(setup, "check_dependencies", lambda: [])
    monkeypatch.setattr(setup.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not install"))
    assert setup.main(["--check"]) == 0
