"""候选模型、会话选择与页面可见的连接状态。"""
from __future__ import annotations
import asyncio
import json
from pathlib import Path

from konden.config import config_path, load_app_config, parse_app_config, write_json
from konden.llm.config import initial_connections, load_connections
from konden.llm.config import ModelConfig
from thinllm import (
    PROVIDERS, InvalidLLMResponse, LLMProviderError, LLMTransientError,
    MissingProviderSetting, list_models, resolve_endpoint,
)


class ModelConfigurationError(ValueError):
    pass


class ModelInUseError(ModelConfigurationError):
    pass


class ModelCatalogUnavailable(RuntimeError):
    pass


class ModelSettings:
    def __init__(self, home, sessions_root: Path, *, path: Path | None = None):
        self.path = config_path() if path is None else path
        self.connections_path = home.connections_path
        config = load_app_config(path)
        if not self.connections_path.exists():
            write_json(self.connections_path, initial_connections())
        load_connections(self.connections_path)
        self._selection_path = sessions_root / "models.json"
        self._selected = (json.loads(self._selection_path.read_text(encoding="utf-8"))
                          if self._selection_path.exists() else {})
        if type(self._selected) is not dict or any(type(key) is not str or not key for key in self._selected):
            raise ValueError("会话模型元数据必须使用非空 session_id")
        # Upgrade the persisted selection once; all reads and writes below use
        # one complete record so model and effort cannot be committed separately.
        if self._selected and all(
            value is None or type(value) is str for value in self._selected.values()
        ):
            self._selected = {sid: self._choice(None if model is None else config.get_model(model))
                              for sid, model in self._selected.items()}
            write_json(self._selection_path, self._selected)
        if any(
            type(value) is not dict
            or set(value) != {"model", "reasoning_effort"}
            or (value["model"] is not None and type(value["model"]) is not str)
            or (value["reasoning_effort"] is not None and type(value["reasoning_effort"]) is not str)
            for value in self._selected.values()
        ):
            raise ValueError("会话模型元数据必须包含 model/reasoning_effort")
        self._applied: dict[str, dict] = {}

    def config(self):
        return load_app_config(self.path)

    @staticmethod
    def _choice(profile: ModelConfig | None) -> dict:
        return {"model": None if profile is None else profile.model,
                "reasoning_effort": None if profile is None else profile.reasoning_effort}

    async def catalog(self, provider: str) -> list[dict]:
        if provider not in PROVIDERS:
            raise ModelConfigurationError("没有这个供应商")
        connections = load_connections(self.connections_path)
        try:
            return await list_models(provider, connections[provider])
        except MissingProviderSetting as error:
            raise ModelConfigurationError(f"请配置 {provider}.{error.setting}") from error
        except (LLMProviderError, LLMTransientError, InvalidLLMResponse) as error:
            raise ModelCatalogUnavailable(str(error)) from error

    async def validate_profiles(self, profiles: list[ModelConfig]) -> None:
        providers = {item.model.partition("/")[0] for item in profiles if item.reasoning_effort is not None}
        catalogs = await asyncio.gather(*(self.catalog(provider) for provider in sorted(providers)))
        entries = {item["model"]: item for catalog in catalogs for item in catalog}
        for profile in profiles:
            if profile.reasoning_effort is None:
                continue
            item = entries.get(profile.model)
            if item is None or profile.reasoning_effort not in item["reasoning_levels"]:
                raise ModelConfigurationError(f"{profile.model} 未提供推理档位 {profile.reasoning_effort}")

    async def test_profile(self, model: str, reasoning_effort: str | None) -> ModelConfig:
        try:
            profile = ModelConfig(model, 50000, reasoning_effort)
        except ValueError as error:
            raise ModelConfigurationError(str(error)) from error
        await self.validate_profiles([profile])
        return profile

    async def save_config(self, data: object) -> dict:
        try:
            updated = parse_app_config(data)
        except ValueError as error:
            raise ModelConfigurationError(str(error)) from error
        await self.validate_profiles(list(updated.models))
        return self.save(data)

    def require_candidate(self, model: str):
        config = self.config()
        try:
            return config.get_model(model)
        except ValueError as error:
            raise ModelConfigurationError(str(error)) from error

    def initialize_session(self, session_id: str, *, parent: str | None = None):
        if session_id in self._selected:
            return
        selected = {**self._selected, session_id: (dict(self._selected[parent]) if parent is not None
                                                  else self._choice(self.config().default))}
        write_json(self._selection_path, selected)
        self._selected = selected

    def selected(self, session_id: str) -> str | None:
        return self._selected[session_id]["model"]

    def _profile(self, session_id: str) -> dict | None:
        selected = self._selected[session_id]
        if selected["model"] is None:
            return None
        candidate = self.config().get_model(selected["model"])
        return ModelConfig(candidate.model, candidate.compact_threshold_tokens, selected["reasoning_effort"]).to_dict()

    def selection(self, session_id: str) -> dict:
        profile = self._profile(session_id)
        return {"selected": profile, "effective": self._applied.get(session_id),
                "pending": profile != self._applied.get(session_id)}

    def for_decision(self, session_id: str, *, apply: bool = True) -> dict | None:
        model = self.selected(session_id)
        if model is None:
            if not apply:
                return None
            raise ModelConfigurationError("请先为会话选择模型")
        profile = self._profile(session_id)
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
        self._require_usable(model)

    def require_default_ready(self):
        """A new Agent decides right after creation, so it needs a usable default first."""
        model = self.config().default_model
        if model is None:
            raise ModelConfigurationError("请先在模型配置中设置默认模型")
        self._require_usable(model)

    def _require_usable(self, model: str):
        self.config().get_model(model)
        provider = model.partition("/")[0]
        status = next(item for item in self.provider_states() if item["provider"] == provider)
        if not status["configured"]:
            raise ModelConfigurationError(f"请先在 connections.json 中配置 {provider}.{status['missing']}")

    def select(self, session_id: str, model: str, reasoning_effort: str | None = None):
        self.selected(session_id)
        self.require_candidate(model)
        provider = model.partition("/")[0]
        status = next(item for item in self.provider_states() if item["provider"] == provider)
        if not status["configured"]:
            raise ModelConfigurationError(f"{provider} 尚未配置 {status['missing']}")
        selected = {**self._selected, session_id: {"model": model, "reasoning_effort": reasoning_effort}}
        write_json(self._selection_path, selected)
        self._selected = selected
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
        users = [sid for sid, choice in self._selected.items() if choice["model"] in removed]
        if users:
            raise ModelInUseError("请先切换以下会话的模型：" + "、".join(users))
        write_json(self.path, updated.to_dict())
        return self.view()
