"""候选模型、会话选择与页面可见的连接状态。"""
from __future__ import annotations
import json
from pathlib import Path

from redpanda.config import config_path, load_app_config, parse_app_config, write_json
from redpanda.llm.config import initial_connections, load_connections
from thinllm import PROVIDERS, MissingProviderSetting, resolve_endpoint


class ModelConfigurationError(ValueError):
    pass


class ModelInUseError(ModelConfigurationError):
    pass


class ModelSettings:
    def __init__(self, home, sessions_root: Path, *, path: Path | None = None):
        self.path = config_path() if path is None else path
        self.connections_path = home.connections_path
        load_app_config(path)
        if not self.connections_path.exists():
            write_json(self.connections_path, initial_connections())
        load_connections(self.connections_path)
        self._selection_path = sessions_root / "models.json"
        self._selected = (json.loads(self._selection_path.read_text(encoding="utf-8"))
                          if self._selection_path.exists() else {})
        if type(self._selected) is not dict or any(
            type(key) is not str or not key or (value is not None and type(value) is not str)
            for key, value in self._selected.items()
        ):
            raise ValueError("会话模型元数据必须是 session_id 到模型名称的映射")
        self._applied: dict[str, dict] = {}

    def config(self):
        return load_app_config(self.path)

    def require_candidate(self, model: str):
        config = self.config()
        try:
            return config.get_model(model)
        except ValueError as error:
            raise ModelConfigurationError(str(error)) from error

    def initialize_session(self, session_id: str, *, parent: str | None = None):
        self._selected[session_id] = (self.selected(parent) if parent is not None
                                      else self.config().default_model)
        write_json(self._selection_path, self._selected)

    def selected(self, session_id: str) -> str | None:
        return self._selected[session_id]

    def selection(self, session_id: str) -> dict:
        selected = self.selected(session_id)
        profile = None if selected is None else self.config().get_model(selected).to_dict()
        return {"selected": profile, "effective": self._applied.get(session_id),
                "pending": profile != self._applied.get(session_id)}

    def for_decision(self, session_id: str, *, apply: bool = True) -> dict | None:
        model = self.selected(session_id)
        if model is None:
            if not apply:
                return None
            raise ModelConfigurationError("请先为会话选择模型")
        profile = self.config().get_model(model).to_dict()
        if apply:
            self._applied[session_id] = profile
        return profile

    def provider_states(self) -> list[dict]:
        connections = load_connections(self.connections_path)
        result = []
        for name, provider in PROVIDERS.items():
            missing = None
            try:
                resolve_endpoint(name + "/probe", connections[name])
            except MissingProviderSetting as error:
                missing = error.setting
            result.append({"provider": name, "configured": missing is None,
                           "missing": missing, "local": provider.base_url is None})
        return result

    def require_ready(self, session_id: str):
        model = self.selected(session_id)
        if model is None:
            raise ModelConfigurationError("请先在会话输入区选择模型")
        self.config().get_model(model)
        provider = model.partition("/")[0]
        status = next(item for item in self.provider_states() if item["provider"] == provider)
        if not status["configured"]:
            raise ModelConfigurationError(f"请先在 connections.json 中配置 {provider}.{status['missing']}")

    def select(self, session_id: str, model: str):
        self.selected(session_id)
        self.require_candidate(model)
        provider = model.partition("/")[0]
        status = next(item for item in self.provider_states() if item["provider"] == provider)
        if not status["configured"]:
            raise ModelConfigurationError(f"{provider} 尚未配置 {status['missing']}")
        self._selected[session_id] = model
        write_json(self._selection_path, self._selected)
        return self.selection(session_id)

    def release_worker(self, session_id: str):
        self._applied.pop(session_id, None)

    def view(self) -> dict:
        return {
            "config": self.config().to_dict(),
            "providers": self.provider_states(),
            "connections_path": str(self.connections_path),
        }

    def save(self, data: object) -> dict:
        current = self.config()
        try:
            updated = parse_app_config(data)
        except ValueError as error:
            raise ModelConfigurationError(str(error)) from error
        removed = {item.model for item in current.models} - {item.model for item in updated.models}
        if removed & {current.default_model}:
            raise ModelInUseError("请先保存新的默认模型，再删除原默认模型")
        users = [sid for sid, model in self._selected.items() if model in removed]
        if users:
            raise ModelInUseError("请先切换以下会话的模型：" + "、".join(users))
        write_json(self.path, updated.to_dict())
        return self.view()
