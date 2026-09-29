"""Application model selection and built-in provider connection settings."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class Provider:
    """A built-in OpenAI-compatible provider; base_url_env marks local deployments."""

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
class ModelConfig:
    """The HelperMe-selected model, written as provider/model."""

    active: str

    def __post_init__(self) -> None:
        if type(self.active) is not str:
            raise ValueError("model.active must be a str")
        active = self.active.strip()
        provider, _, model = active.partition("/")
        if provider not in PROVIDERS or not model:
            raise ValueError(
                "model.active 必须形如 <provider>/<model>，provider 为 "
                + "、".join(PROVIDERS)
            )
        object.__setattr__(self, "active", active)

    @property
    def provider(self) -> str:
        return self.active.partition("/")[0]


@dataclass(frozen=True, slots=True)
class Endpoint:
    """Resolved connection details for the selected provider."""

    provider: str
    base_url: str
    api_key: str | None
    pads_reasoning_content: bool


def load_endpoint(model: ModelConfig) -> Endpoint:
    """Load the project .env for the selected provider; OS environment wins."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    provider = PROVIDERS[model.provider]
    if provider.base_url_env is None:
        base_url = provider.base_url
    else:
        base_url = os.environ.get(provider.base_url_env, "").strip()
        if not base_url:
            raise ValueError(f"请在项目 .env 中设置 {provider.base_url_env}")
    api_key = os.environ.get(provider.api_key_env, "").strip() or None
    if api_key is None and provider.api_key_required:
        raise ValueError(f"请在项目 .env 中设置 {provider.api_key_env}")
    return Endpoint(
        provider=model.provider,
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        pads_reasoning_content=provider.pads_reasoning_content,
    )
