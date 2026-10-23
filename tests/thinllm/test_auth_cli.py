import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from thinllm import __main__ as cli


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    root = tmp_path / "Codex 登录 示例"
    root.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(root))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    return root


def write_login(root, token="private-access"):
    (root / "auth.json").write_text(json.dumps({
        "auth_mode": "chatgpt", "tokens": {"access_token": token, "account_id": "private-account"},
    }), encoding="utf-8")


def test_existing_login_exports_only_a_reference_without_cli_or_file_writes(codex_home, monkeypatch, capsys):
    write_login(codex_home)
    auth = codex_home / "auth.json"
    before = (auth.read_bytes(), auth.stat().st_mtime_ns)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli.shutil, "which", lambda _: pytest.fail("existing login needs no Codex CLI"))
    assert cli.main(["auth", "codex"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {"openai-codex": {"auth_file": auth.resolve().as_posix()}}
    assert "private-access" not in output.out + output.err
    assert "private-account" not in output.out + output.err
    assert output.err == ""
    assert (auth.read_bytes(), auth.stat().st_mtime_ns) == before


@pytest.mark.parametrize("existing", [None, {"auth_mode": "apikey", "OPENAI_API_KEY": "private-key"}])
def test_login_delegates_to_codex_and_keeps_stdout_as_copyable_json(codex_home, monkeypatch, capsys, existing):
    if existing is not None:
        (codex_home / "auth.json").write_text(json.dumps(existing), encoding="utf-8")
    config = codex_home / "config.toml"
    config.write_text('cli_auth_credentials_store = "keyring"\n', encoding="utf-8")
    before = config.read_bytes()
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/tools/codex")

    def login(command, **options):
        assert command == ["/tools/codex", "-c", 'cli_auth_credentials_store="file"',
                           "-c", 'forced_login_method="chatgpt"', "login"]
        assert options == {"stdout": cli.sys.stderr, "check": False}
        print("浏览器授权完成", file=options["stdout"])
        write_login(codex_home)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", login)
    assert cli.main(["auth", "codex"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["openai-codex"]["auth_file"] == (codex_home / "auth.json").resolve().as_posix()
    assert "浏览器授权完成" in output.err
    assert "private-" not in output.out + output.err
    assert config.read_bytes() == before


def test_explicit_login_replaces_an_existing_session_through_codex(codex_home, monkeypatch, capsys):
    write_login(codex_home, "old-token")
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/tools/codex")
    calls = []

    def login(*args, **kwargs):
        calls.append(args)
        write_login(codex_home, "new-token")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", login)
    assert cli.main(["auth", "codex", "--login"]) == 0
    assert len(calls) == 1
    assert json.loads(capsys.readouterr().out)["openai-codex"]["auth_file"]


@pytest.mark.parametrize(("interactive", "available", "message"), [
    (False, True, "交互终端"), (True, False, "安装"),
])
def test_missing_prerequisites_fail_without_waiting_or_printing_config(codex_home, monkeypatch, capsys,
                                                                     interactive, available, message):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: interactive)
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/tools/codex" if available else None)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: pytest.fail("must not start login"))
    assert cli.main(["auth", "codex"]) == 1
    output = capsys.readouterr()
    assert output.out == "" and message in output.err
    assert not (codex_home / "auth.json").exists()


@pytest.mark.parametrize("exit_code", [0, 1])
def test_failed_or_incomplete_login_never_exports_a_stale_reference(codex_home, monkeypatch, capsys, exit_code):
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/tools/codex")
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=exit_code))
    assert cli.main(["auth", "codex"]) == 1
    output = capsys.readouterr()
    assert output.out == "" and output.err


def test_corrupt_persisted_auth_is_exposed_without_starting_login(codex_home, monkeypatch):
    auth = codex_home / "auth.json"
    auth.write_text("{", encoding="utf-8")
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: pytest.fail("must not replace damaged auth"))
    with pytest.raises(json.JSONDecodeError):
        cli.main(["auth", "codex"])
    assert auth.read_text(encoding="utf-8") == "{"


def test_cancelled_login_exits_without_configuration(codex_home, monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/tools/codex")

    def cancel(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.subprocess, "run", cancel)
    assert cli.main(["auth", "codex"]) == 130
    output = capsys.readouterr()
    assert output.out == "" and "取消" in output.err


@pytest.mark.process
def test_python_module_command_exports_valid_configuration(codex_home):
    write_login(codex_home)
    result = subprocess.run(
        [sys.executable, "-m", "thinllm", "auth", "codex"],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "CODEX_HOME": str(codex_home)},
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    assert json.loads(result.stdout) == {"openai-codex": {
        "auth_file": (codex_home / "auth.json").resolve().as_posix(),
    }}
    assert result.stderr == "" and "private-" not in result.stdout
