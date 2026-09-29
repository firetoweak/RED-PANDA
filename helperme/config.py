"""HelperMe 应用配置。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from helperme.llm.api import LLMApi
from helperme.llm.config import ModelConfig
from helperme.paths import HelperMeHome


CONFIG_PATH_ENV = "HELPERME_CONFIG"
INITIAL_CONFIG = {
    "model": {
        "active": "deepseek/deepseek-v4-pro",
    },
    "runtime": {
        "compact_threshold_tokens": 200000,
    },
}


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    compact_threshold_tokens: int


@dataclass(frozen=True, slots=True)
class AppConfig:
    model: ModelConfig
    runtime: RuntimeConfig


@dataclass(frozen=True, slots=True)
class AssistantConfig:
    model_name: str
    compact_threshold_tokens: int
    llm: LLMApi


def _create_initial_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as config_file:
        json.dump(INITIAL_CONFIG, config_file, ensure_ascii=False, indent=2)
        config_file.write("\n")


def _load_config_data(path: Path | None) -> dict:
    uses_default_path = path is None and CONFIG_PATH_ENV not in os.environ
    if path is not None:
        config_path = path
    elif CONFIG_PATH_ENV in os.environ:
        config_path = Path(os.environ[CONFIG_PATH_ENV])
    else:
        config_path = HelperMeHome.default().config_path
    if not config_path.is_file():
        if not uses_default_path:
            raise FileNotFoundError(f"配置不存在：{config_path}")
        _create_initial_config(config_path)
    with config_path.open("r", encoding="utf-8") as config_file:
        data = json.load(config_file)
    if not isinstance(data, dict):
        raise ValueError("配置必须是 JSON object")
    return data


def _parse_model_config(data: dict) -> ModelConfig:
    model = data["model"]
    if not isinstance(model, dict):
        raise ValueError("模型配置必须包含 model 映射")
    if set(model) != {"active"}:
        raise ValueError("模型配置字段必须只有 active")
    return ModelConfig(active=model["active"])


def load_app_config(path: Path | None = None) -> AppConfig:
    data = _load_config_data(path)
    if set(data) != {"model", "runtime"}:
        raise ValueError("配置字段必须是 model/runtime")

    runtime = data["runtime"]
    if not isinstance(runtime, dict):
        raise ValueError("配置必须包含 runtime 映射")
    if set(runtime) != {"compact_threshold_tokens"}:
        raise ValueError("runtime 配置字段必须是 compact_threshold_tokens")
    compact_threshold_tokens = runtime["compact_threshold_tokens"]
    if type(compact_threshold_tokens) is not int or compact_threshold_tokens <= 0:
        raise ValueError("配置 runtime.compact_threshold_tokens 必须是大于 0 的整数")

    return AppConfig(
        model=_parse_model_config(data),
        runtime=RuntimeConfig(
            compact_threshold_tokens=compact_threshold_tokens,
        ),
    )


def assistant_config_from_app(app: AppConfig, llm: LLMApi) -> AssistantConfig:
    return AssistantConfig(
        model_name=app.model.active,
        compact_threshold_tokens=app.runtime.compact_threshold_tokens,
        llm=llm,
    )
