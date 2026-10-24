import json

import pytest

from konden import initialize
from konden.config import config_path, load_app_config, write_json
from konden.llm.config import load_connections


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KONDEN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("KONDEN_CONFIG", raising=False)
    return initialize.initialize()


def inputs(monkeypatch, values, *, key=""):
    answers = iter(values)
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(initialize.getpass, "getpass", lambda _: key)


def interactive(monkeypatch):
    monkeypatch.setattr(initialize.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(initialize.sys.stdout, "isatty", lambda: True)


def test_initialize_preserves_existing_files_and_rejects_corruption(home):
    paths = (config_path(), home.connections_path)
    before = [path.read_bytes() for path in paths]
    initialize.initialize()
    assert [path.read_bytes() for path in paths] == before
    assert load_app_config().default is None
    home.connections_path.write_text("{", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        initialize.initialize()
    assert home.connections_path.read_text(encoding="utf-8") == "{"


def test_wizard_adds_default_without_replacing_other_models_or_credentials(home, monkeypatch, capsys):
    existing = {"model": "stepfun/step-5-preview", "compact_threshold_tokens": 64000,
                "reasoning_effort": "high"}
    write_json(config_path(), {"model": {"default": existing["model"], "candidates": [existing]}})
    connections = load_connections(home.connections_path)
    connections["stepfun"]["api_key"] = "keep-this-key"
    write_json(home.connections_path, connections)
    inputs(monkeypatch, ["deepseek", "my-model", "", "0", "invalid", "64000"], key="new-secret")

    profile = initialize.configure(home)

    assert profile.model == "deepseek/my-model"
    assert load_app_config().to_dict() == {"model": {"default": profile.model,
                                                 "candidates": [existing, profile.to_dict()]}}
    updated = load_connections(home.connections_path)
    assert updated["deepseek"]["api_key"] == "new-secret"
    assert updated["stepfun"] == connections["stepfun"]
    assert "new-secret" not in capsys.readouterr().out


def test_local_model_accepts_empty_key_and_retries_invalid_address(home, monkeypatch):
    inputs(monkeypatch, ["vllm", "not-a-url", "http://localhost:9000/v1", "org/code-model", "32000"])
    profile = initialize.configure(home)
    assert profile.model == "vllm/org/code-model"
    assert load_connections(home.connections_path)["vllm"] == {
        "api_key": "", "base_url": "http://localhost:9000/v1",
    }


def test_codex_wizard_reuses_login_without_asking_for_or_copying_secrets(home, tmp_path, monkeypatch):
    auth_file = tmp_path / "auth.json"
    write_json(auth_file, {"tokens": {"access_token": "codex-secret", "account_id": "account"}})
    before = auth_file.read_bytes()
    inputs(monkeypatch, ["openai-codex", str(auth_file), "test-model", "64000"])
    monkeypatch.setattr(initialize.getpass, "getpass", lambda _: pytest.fail("must not ask for API key"))
    profile = initialize.configure(home)
    assert profile.model == "openai-codex/test-model"
    assert load_connections(home.connections_path)["openai-codex"] == {"auth_file": str(auth_file)}
    assert "codex-secret" not in home.connections_path.read_text(encoding="utf-8")
    assert auth_file.read_bytes() == before


def test_editing_existing_model_preserves_key_and_reasoning_effort(home, monkeypatch, capsys):
    previous = {"model": "stepfun/step-5-preview", "compact_threshold_tokens": 64000,
                "reasoning_effort": "high"}
    write_json(config_path(), {"model": {"default": previous["model"], "candidates": [previous]}})
    connections = load_connections(home.connections_path)
    connections["stepfun"]["api_key"] = "existing-secret"
    write_json(home.connections_path, connections)
    inputs(monkeypatch, ["", "", "", ""])
    profile = initialize.configure(home)
    assert profile.to_dict() == previous
    assert load_app_config().models == (profile,)
    assert load_connections(home.connections_path)["stepfun"]["api_key"] == "existing-secret"
    assert "existing-secret" not in capsys.readouterr().out


def test_configuration_respects_explicit_model_config_path(home, tmp_path, monkeypatch):
    original = home.config_path.read_bytes()
    target = tmp_path / "models.json"
    write_json(target, {"model": {"default": None, "candidates": []}})
    monkeypatch.setenv("KONDEN_CONFIG", str(target))
    inputs(monkeypatch, ["ollama", "", "my-model", "32000"])
    initialize.configure(home)
    assert load_app_config().default_model == "ollama/my-model"
    assert home.config_path.read_bytes() == original


def test_if_needed_never_prompts_without_terminal(home, monkeypatch, capsys):
    monkeypatch.setattr(initialize.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("must not prompt"))
    assert initialize.main(["--if-needed"]) == 0
    assert "--configure" in capsys.readouterr().out
    assert load_app_config().default is None
    with pytest.raises(SystemExit) as error:
        initialize.main(["--configure"])
    assert error.value.code == 2


def test_if_needed_preserves_configured_default_without_prompting(home, monkeypatch):
    inputs(monkeypatch, ["ollama", "", "code", "32000"])
    initialize.configure(home)
    before = [path.stat().st_mtime_ns for path in (config_path(), home.connections_path)]
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("must not prompt"))
    assert initialize.main(["--if-needed"]) == 0
    assert [path.stat().st_mtime_ns for path in (config_path(), home.connections_path)] == before


def test_cancel_before_saving_preserves_config(home, monkeypatch):
    before = [path.read_bytes() for path in (config_path(), home.connections_path)]
    interactive(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert initialize.main(["--configure"]) == 130
    assert [path.read_bytes() for path in (config_path(), home.connections_path)] == before


def test_failed_optional_test_keeps_saved_model(home, monkeypatch, capsys):
    interactive(monkeypatch)
    inputs(monkeypatch, ["ollama", "", "code", "32000", "y"])

    async def unavailable(_home, profile):
        assert profile.model == "ollama/code"
        return {"ok": False, "message": "服务未启动", "elapsed_ms": 1}

    monkeypatch.setattr(initialize, "_test_model", unavailable)
    assert initialize.main(["--configure"]) == 1
    assert load_app_config().default_model == "ollama/code"
    assert "服务未启动" in capsys.readouterr().out
