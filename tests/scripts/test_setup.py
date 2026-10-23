import pytest

from scripts import setup


def test_preflight_collects_missing_project_tools_and_node_version(monkeypatch):
    monkeypatch.setattr(setup.shutil, "which", lambda command: "node" if command == "node" else None)
    monkeypatch.setattr(setup.runtime_tools, "supported_node", lambda: False)
    message = "\n".join(setup.check_dependencies())
    for dependency in ("Git", "ripgrep", "fd", "npm", "Node.js 版本"):
        assert dependency in message


def test_failed_check_never_starts_installation(monkeypatch):
    monkeypatch.setattr(setup.runtime_tools, "activate", lambda root: None)
    monkeypatch.setattr(setup.runtime_tools, "prepare", lambda root: None)
    monkeypatch.setattr(setup, "check_dependencies", lambda: ["missing Git"])
    monkeypatch.setattr(setup.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not install"))
    with pytest.raises(SystemExit) as error:
        setup.main(["--check"])
    assert error.value.code == 1


@pytest.mark.parametrize("arguments,configure", [([], False), (["--configure"], True)])
def test_preparation_uses_project_environment_and_configures_only_when_requested(monkeypatch, arguments, configure):
    monkeypatch.setattr(setup, "check_dependencies", lambda: [])
    monkeypatch.setattr(setup.runtime_tools, "prepare", lambda root: None)
    monkeypatch.setattr(setup.shutil, "which", lambda _: "npm")
    commands = []
    monkeypatch.setattr(setup.subprocess, "run", lambda command, **kwargs: commands.append((command, kwargs)))
    assert setup.main(arguments) == 0
    assert len(commands) == 6
    assert commands[0][0][1:] == ["pip", "install", "--python", setup.sys.executable, "pip"]
    assert commands[1][0][1:] == ["pip", "install", "--python", setup.sys.executable,
                                  "-r", "requirements.lock", "--require-hashes"]
    assert commands[2][0] == ["npm", "--prefix", "scripts/browser", "ci"]
    assert commands[3][0] == ["npm", "--prefix", "web", "ci"]
    assert commands[4][0] == ["npm", "--prefix", "web", "run", "build"]
    assert commands[5][0] == [setup.sys.executable, "-m", "redpanda.initialize"] + (["--configure"] if configure else [])
    assert all(kwargs == {"cwd": setup.ROOT, "check": True} for _, kwargs in commands)


def test_project_preparation_does_not_invoke_rust_or_llvm(monkeypatch):
    monkeypatch.setattr(setup.runtime_tools, "prepare", lambda root: None)
    monkeypatch.setattr(setup.runtime_tools, "supported_node", lambda: True)
    project_tools = {"git", "rg", "fd", "node", "npm", "npm.cmd"}
    monkeypatch.setattr(setup.shutil, "which", lambda command: command if command in project_tools else None)
    commands = []

    def run(command, **kwargs):
        commands.append(command)

    monkeypatch.setattr(setup.subprocess, "run", run)
    assert setup.main([]) == 0
    assert len(commands) == 6
    assert not any("cargo" in part or "rustup" in part for command in commands for part in map(str, command))


def test_check_mode_only_validates_dependencies(monkeypatch):
    monkeypatch.setattr(setup.runtime_tools, "activate", lambda root: None)
    monkeypatch.setattr(setup.runtime_tools, "prepare", lambda root: pytest.fail("must not install tools"))
    monkeypatch.setattr(setup, "check_dependencies", lambda: [])
    monkeypatch.setattr(setup.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not install"))
    assert setup.main(["--check"]) == 0
