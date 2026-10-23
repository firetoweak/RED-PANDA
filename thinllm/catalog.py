"""Account-specific Codex model choices; no local catalog or CLI process."""
from __future__ import annotations

from collections.abc import Mapping

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from thinllm.auth import CodexAuth, codex_auth_path
from thinllm.errors import InvalidLLMResponse, LLMAuthenticationError, LLMProviderError, LLMTransientError
from thinllm.providers import PROVIDERS

# The catalog endpoint requires the Codex catalog protocol's client version.
# https://github.com/openai/codex/blob/main/codex-rs/codex-api/src/endpoint/models.rs
_CATALOG_CLIENT_VERSION = "0.164.0"


class _ReasoningLevel(BaseModel):
    model_config = ConfigDict(strict=True)
    effort: str = Field(min_length=1)


class _Model(BaseModel):
    model_config = ConfigDict(strict=True)
    slug: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    context_window: int | None = Field(gt=0)
    default_reasoning_level: str | None
    supported_reasoning_levels: list[_ReasoningLevel]
    visibility: str = Field(min_length=1, exclude=True)


class _Catalog(BaseModel):
    model_config = ConfigDict(strict=True)
    models: list[_Model]


async def list_codex_models(connection: Mapping[str, str], *, transport: httpx.AsyncBaseTransport | None = None) -> list[dict]:
    """Return picker-visible models using the same credentials as inference."""
    path = codex_auth_path(connection["auth_file"]).resolve()
    try:
        async with httpx.AsyncClient(
            base_url=PROVIDERS["openai-codex"].base_url + "/",
            auth=CodexAuth(path), headers={"originator": "thinllm"}, timeout=30, transport=transport,
        ) as client:
            response = await client.get("models", params={"client_version": _CATALOG_CLIENT_VERSION})
    except httpx.TransportError as error:
        raise LLMTransientError("Codex 模型列表连接失败，请重试。") from error
    if response.status_code in (401, 403):
        raise LLMAuthenticationError(
            "无法读取 Codex 模型列表，请运行 python -m thinllm auth codex --login 并更新连接配置。")
    if response.status_code in (408, 425, 429, 500, 502, 503, 504):
        raise LLMTransientError(f"Codex 模型列表暂时不可用（HTTP {response.status_code}），请重试。")
    if response.status_code != 200:
        raise LLMProviderError(f"读取 Codex 模型列表失败（HTTP {response.status_code}）。")
    try:
        catalog = _Catalog.model_validate_json(response.content)
    except ValidationError as error:
        raise InvalidLLMResponse("invalid_model_catalog", "Codex 模型列表格式不符合约定。") from error
    return [model.model_dump() for model in catalog.models if model.visibility == "list"]
