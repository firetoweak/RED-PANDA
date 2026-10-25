from subprocess import CompletedProcess

import pytest

from scripts import setup


def test_preflight_collects_missing_project_tools_and_node_version(monkeypatch):
    monkeypatch.setattr(setup.shutil, "which", lambda command: "node" if command == "node" else None)
    monkeypatch.setattr(setup.runtime_tools, "supported_node", lambda: False)
    message = "\n".join(setup.check_dependencies())
    for dependency in ("Git", "ripgrep", "fd", "npm", "Node.js 版本"):
        assert dependency in message


@pytest.mark.parametrize("arguments,configure", [([], False), (["--configure"], True)])
def test_preparation_uses_locked_dependencies_and_configures_only_when_requested(monkeypatch, arguments, configure):
    monkeypatch.setattr(setup, "check_dependencies", lambda: [])
    monkeypatch.setattr(setup.runtime_tools, "prepare", lambda root: None)
    monkeypatch.setattr(setup.shutil, "which", lambda _: "npm")
    commands = []
    monkeypatch.setattr(setup.subprocess, "run", lambda command, **kwargs: (commands.append((command, kwargs)) or CompletedProcess(command, 0)))
    assert setup.main(arguments) == 0
    install, = [(command, kwargs) for command, kwargs in commands if "--require-hashes" in command]
    command, options = install
    assert command[command.index("--python") + 1] == setup.sys.executable
    assert command[command.index("-r") + 1] == "requirements.lock"
    assert options == {"cwd": setup.ROOT, "check": True}
    initialize, = [command for command, _ in commands if "konden.initialize" in command]
    assert ("--configure" in initialize) == configure


@pytest.mark.parametrize("missing", [[], ["missing Git"]])
def test_check_mode_reports_dependencies_without_installing(monkeypatch, missing):
    monkeypatch.setattr(setup.runtime_tools, "activate", lambda root: None)
    monkeypatch.setattr(setup.runtime_tools, "prepare", lambda root: pytest.fail("must not install tools"))
    monkeypatch.setattr(setup, "check_dependencies", lambda: missing)
    monkeypatch.setattr(setup.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not install"))
    if missing:
        with pytest.raises(SystemExit) as error:
            setup.main(["--check"])
        assert error.value.code == 1
    else:
        assert setup.main(["--check"]) == 0
