"""内置 Provider 的协议事实；调用方提供结构化连接设置。"""
from __future__ import annotations
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Provider:
    base_url: str | None = None
    api_key_required: bool = True
    pads_reasoning_content: bool = False


PROVIDERS = {
    "deepseek": Provider("https://api.deepseek.com/v1", pads_reasoning_content=True),
    "openai": Provider("https://api.openai.com/v1"),
    "qwen": Provider("https://dashscope.aliyuncs.com/compatible-mode/v1"),
    "stepfun": Provider("https://api.stepfun.com/step_plan/v1"),
    "bigmodel": Provider("https://open.bigmodel.cn/api/paas/v4"),
    "vllm": Provider(api_key_required=False),
    "ollama": Provider(api_key_required=False),
}


@dataclass(frozen=True, slots=True)
class Endpoint:
    provider: str
    base_url: str
    api_key: str | None
    pads_reasoning_content: bool


class MissingProviderSetting(ValueError):
    def __init__(self, setting: str):
        self.setting = setting
        super().__init__(f"{setting} is required")


def resolve_endpoint(model: str, connection: Mapping[str, str]) -> Endpoint:
    name = model.partition("/")[0]
    provider = PROVIDERS[name]
    base_url = provider.base_url
    if base_url is None:
        base_url = connection["base_url"].strip()
        if not base_url:
            raise MissingProviderSetting("base_url")
    api_key = connection["api_key"].strip() or None
    if api_key is None and provider.api_key_required:
        raise MissingProviderSetting("api_key")
    return Endpoint(name, base_url.rstrip("/"), api_key, provider.pads_reasoning_content)
