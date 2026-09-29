"""Built-in OpenAI-compatible providers addressed by provider/model identifiers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Provider:
    """A built-in provider; base_url_env marks local deployments."""

    api_key_env: str
    base_url: str | None = None
    base_url_env: str | None = None
    api_key_required: bool = True
    pads_reasoning_content: bool = False


PROVIDERS = {
    "deepseek": Provider(
        api_key_env="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com/v1",
        pads_reasoning_content=True,
    ),
    "qwen": Provider(
        api_key_env="QWEN_API_KEY",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    ),
    "vllm": Provider(
        api_key_env="VLLM_API_KEY",
        base_url_env="VLLM_BASE_URL",
        api_key_required=False,
    ),
}


@dataclass(frozen=True, slots=True)
class Endpoint:
    """Resolved connection details for one provider."""

    provider: str
    base_url: str
    api_key: str | None
    pads_reasoning_content: bool


class MissingProviderSetting(ValueError):
    def __init__(self, variable: str) -> None:
        super().__init__(f"{variable} is required")
        self.variable = variable


def resolve_endpoint(model: str, environ: Mapping[str, str]) -> Endpoint:
    """Resolve a provider/model identifier against settings in environ."""
    name = model.partition("/")[0]
    provider = PROVIDERS[name]
    if provider.base_url_env is None:
        base_url = provider.base_url
    else:
        base_url = environ.get(provider.base_url_env, "").strip()
        if not base_url:
            raise MissingProviderSetting(provider.base_url_env)
    api_key = environ.get(provider.api_key_env, "").strip() or None
    if api_key is None and provider.api_key_required:
        raise MissingProviderSetting(provider.api_key_env)
    return Endpoint(
        provider=name,
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        pads_reasoning_content=provider.pads_reasoning_content,
    )
