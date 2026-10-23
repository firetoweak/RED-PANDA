"""内置 Provider 的协议事实；调用方提供结构化连接设置。"""
from __future__ import annotations
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from thinllm.auth import codex_auth_path, load_codex_auth
from thinllm.errors import MissingProviderSetting


@dataclass(frozen=True, slots=True)
class Provider:
    base_url: str | None = None
    api_key_required: bool = True
    pads_reasoning_content: bool = False
    uses_codex_auth: bool = False


PROVIDERS = {
    "deepseek": Provider("https://api.deepseek.com/v1", pads_reasoning_content=True),
    "openai": Provider("https://api.openai.com/v1"),
    "openai-codex": Provider("https://chatgpt.com/backend-api/codex", uses_codex_auth=True),
    "qwen": Provider("https://dashscope.aliyuncs.com/compatible-mode/v1"),
    "stepfun": Provider("https://api.stepfun.com/step_plan/v1"),
    "bigmodel": Provider("https://open.bigmodel.cn/api/paas/v4"),
    "vllm": Provider(api_key_required=False),
    "ollama": Provider(api_key_required=False),
}


# Only models whose hosted API documents preserved thinking are listed here.
# https://docs.z.ai/guides/capabilities/thinking-mode
_GLM_PRESERVED_THINKING_MODELS = frozenset({
    "glm-4.7", "glm-4.7-flash", "glm-4.7-flashx",
    "glm-5", "glm-5.1", "glm-5.2", "glm-5.3", "glm-5.3-flash", "glm-5.3-flashx",
})
# https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions
_QWEN_PRESERVED_THINKING_MODELS = frozenset({
    "qwen3.6-plus", "qwen3.6-plus-2026-04-02",
    "qwen3.6-flash", "qwen3.6-flash-2026-04-16",
    "qwen3.7-max", "qwen3.7-max-2026-05-20",
    "qwen3.7-plus", "qwen3.7-plus-2026-05-26",
    "qwen3.7-flash", "qwen3.7-flash-2026-07-15",
    "qwen3.8-max", "qwen3.8-max-0902", "qwen3.8-flash", "qwen3.8-omni-flash",
})


def preserved_thinking_options(provider: str, model: str) -> dict[str, object]:
    """Enable native history reuse without changing messages or forcing thinking on."""
    if provider == "bigmodel" and model in _GLM_PRESERVED_THINKING_MODELS:
        return {"thinking": {"clear_thinking": False}}
    if provider == "qwen" and model in _QWEN_PRESERVED_THINKING_MODELS:
        return {"preserve_thinking": True}
    return {}


@dataclass(frozen=True, slots=True)
class Endpoint:
    provider: str
    base_url: str
    api_key: str | None
    pads_reasoning_content: bool
    account_id: str | None = None
    auth_file: Path | None = None


def resolve_endpoint(model: str, connection: Mapping[str, str]) -> Endpoint:
    name = model.partition("/")[0]
    provider = PROVIDERS[name]
    if provider.uses_codex_auth:
        path = codex_auth_path(connection["auth_file"]).resolve()
        access_token, account_id = load_codex_auth(path)
        return Endpoint(name, provider.base_url, access_token, False, account_id, path)
    base_url = provider.base_url
    if base_url is None:
        base_url = connection["base_url"].strip()
        if not base_url:
            raise MissingProviderSetting("base_url")
    api_key = connection["api_key"].strip() or None
    if api_key is None and provider.api_key_required:
        raise MissingProviderSetting("api_key")
    return Endpoint(name, base_url.rstrip("/"), api_key, provider.pads_reasoning_content)
