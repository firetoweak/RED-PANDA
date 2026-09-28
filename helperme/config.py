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
        "active": "deepseek-v4-pro",
        "router": {
            "model_list": [
                {
                    "model_name": "deepseek-v4-pro",
                    "litellm_params": {
                        "model": "deepseek/deepseek-v4-pro",
                        "api_key": "your-api-key",
                        "reasoning_effort": "high",
                    },
                }
            ],
        },
    },
    "runtime": {
        "compact_threshold_tokens": 200000,
        "loop_guard_repeat_threshold": 3,
    },
    "channels": {
        "telegram": {
            "bot_token": "your-bot-token",
            "allowed_chat_id": None,
        }
    },
}


# 重试与超时的代码默认值：不写入用户配置，避免用户漏配或写错。
# num_retries 是"首次调用之外"的重试次数；timeout 是单次尝试的超时秒数（流式下即多久没有数据算超时）。
DEFAULT_ROUTER_SETTINGS = {
    "num_retries": 3,
    "timeout": 60,
}


class InitialConfigCreated(RuntimeError):
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"已创建初始配置：{path}；请填写后重新启动")


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    compact_threshold_tokens: int
    loop_guard_repeat_threshold: int = 3


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    bot_token: str
    allowed_chat_id: int | None


@dataclass(frozen=True, slots=True)
class ChannelsConfig:
    telegram: TelegramConfig | None


@dataclass(frozen=True, slots=True)
class AppConfig:
    model: ModelConfig
    runtime: RuntimeConfig
    channels: ChannelsConfig


@dataclass(frozen=True, slots=True)
class AssistantConfig:
    model_name: str
    compact_threshold_tokens: int
    llm: LLMApi
    loop_guard_repeat_threshold: int = 3


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
        if uses_default_path:
            _create_initial_config(config_path)
            raise InitialConfigCreated(config_path)
        raise FileNotFoundError(
            f"配置不存在：{config_path}；请复制 "
            "config.example.json 到该位置并填写真实配置"
        )
    with config_path.open("r", encoding="utf-8") as config_file:
        data = json.load(config_file)
    if not isinstance(data, dict):
        raise ValueError("配置必须是 JSON object")
    return data


def _parse_model_config(data: dict) -> ModelConfig:
    model = data["model"]
    if not isinstance(model, dict):
        raise ValueError("模型配置必须包含 model 映射")
    if set(model) != {"active", "router"}:
        raise ValueError("模型配置字段必须是 active/router")
    active = model["active"]
    if type(active) is not str or not active.strip():
        raise ValueError("模型配置 model.active 不能为空")
    router = model["router"]
    if type(router) is not dict or not router:
        raise ValueError("模型配置 model.router 必须是非空映射")
    for key, value in DEFAULT_ROUTER_SETTINGS.items():
        router.setdefault(key, value)
    return ModelConfig(active=active.strip(), router=router)


def load_app_config(path: Path | None = None) -> AppConfig:
    data = _load_config_data(path)
    if set(data) != {"model", "runtime", "channels"}:
        raise ValueError("配置字段必须是 model/runtime/channels")

    runtime = data["runtime"]
    if not isinstance(runtime, dict):
        raise ValueError("配置必须包含 runtime 映射")
    if set(runtime) != {
        "compact_threshold_tokens",
        "loop_guard_repeat_threshold",
    }:
        raise ValueError(
            "runtime 配置字段必须是 compact_threshold_tokens/loop_guard_repeat_threshold"
        )
    compact_threshold_tokens = runtime["compact_threshold_tokens"]
    if type(compact_threshold_tokens) is not int or compact_threshold_tokens <= 0:
        raise ValueError("配置 runtime.compact_threshold_tokens 必须是大于 0 的整数")

    if type(runtime["loop_guard_repeat_threshold"]) is not int or runtime["loop_guard_repeat_threshold"] < 2:
        raise ValueError("runtime.loop_guard_repeat_threshold must be an integer >= 2")

    channels = data["channels"]
    if not isinstance(channels, dict):
        raise ValueError("配置必须包含 channels 映射")
    if not set(channels) <= {"telegram"}:
        raise ValueError("channels 配置只允许 telegram")
    telegram_config = None
    if "telegram" in channels:
        telegram = channels["telegram"]
        if not isinstance(telegram, dict):
            raise ValueError("channels.telegram 必须是映射")
        if set(telegram) != {"bot_token", "allowed_chat_id"}:
            raise ValueError("channels.telegram 字段必须是 bot_token/allowed_chat_id")
        bot_token = telegram["bot_token"]
        if not isinstance(bot_token, str) or not bot_token.strip():
            raise ValueError("配置 channels.telegram.bot_token 不能为空")
        allowed_chat_id = telegram["allowed_chat_id"]
        if allowed_chat_id is not None and type(allowed_chat_id) is not int:
            raise ValueError("配置 channels.telegram.allowed_chat_id 必须是整数或 null")
        telegram_config = TelegramConfig(
            bot_token=bot_token.strip(),
            allowed_chat_id=allowed_chat_id,
        )

    return AppConfig(
        model=_parse_model_config(data),
        runtime=RuntimeConfig(
            compact_threshold_tokens=compact_threshold_tokens,
            loop_guard_repeat_threshold=runtime["loop_guard_repeat_threshold"],
        ),
        channels=ChannelsConfig(telegram=telegram_config),
    )


def assistant_config_from_app(app: AppConfig, llm: LLMApi) -> AssistantConfig:
    return AssistantConfig(
        model_name=app.model.active,
        compact_threshold_tokens=app.runtime.compact_threshold_tokens,
        llm=llm,
        loop_guard_repeat_threshold=app.runtime.loop_guard_repeat_threshold,
    )
