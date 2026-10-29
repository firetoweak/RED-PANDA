"""Account-specific Codex model choices; no local catalog or CLI process."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from thinllm.auth import CodexAuth, codex_auth_path
from thinllm.errors import InvalidLLMResponse, LLMAuthenticationError, LLMProviderError, LLMTransientError
from thinllm.providers import PROVIDERS, resolve_endpoint
from thinllm.model_metadata import openai_conversation_model, openai_metadata

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


class _Effort(BaseModel):
    model_config = ConfigDict(strict=True)
    supported_levels: list[str]
    default_level: str | None = None


class _ApiModel(BaseModel):
    model_config = ConfigDict(strict=True)
    id: str = Field(min_length=1)
    name: str | None = None
    context_window: int | None = Field(default=None, gt=0)
    max_input_tokens: int | None = Field(default=None, gt=0)
    effort: _Effort | None = None
    reasoning_effort_support_list: list[str] | None = None
    supported_protocols: list[str] | None = None
    output_modalities: list[str] | None = None
    shutdown_date: str | None = None

    @field_validator("shutdown_date")
    @classmethod
    def shutdown_day(cls, value):
        if value is not None:
            date.fromisoformat(value)
        return value


class _ApiCatalog(BaseModel):
    model_config = ConfigDict(strict=True)
    data: list[_ApiModel]


async def list_models(provider: str, connection: Mapping[str, str], *,
                      transport: httpx.AsyncBaseTransport | None = None) -> list[dict]:
    """Discover account-visible conversation models and native reasoning levels."""
    if provider == "openai-codex":
        return [{"model": provider + "/" + item["slug"], "name": item["display_name"],
                 "context_window": item["context_window"],
                 "reasoning_levels": [level["effort"] for level in item["supported_reasoning_levels"]],
                 "default_reasoning_effort": item["default_reasoning_level"], "web_search": False}
                for item in await list_codex_models(connection, transport=transport)]
    endpoint = resolve_endpoint(provider + "/catalog", connection)
    headers = {} if endpoint.api_key is None else {"Authorization": f"Bearer {endpoint.api_key}"}
    try:
        async with httpx.AsyncClient(base_url=endpoint.base_url + "/", headers=headers,
                                    transport=transport, timeout=30) as client:
            response = await client.get("models")
    except httpx.TransportError as error:
        raise LLMTransientError(f"{provider} 模型目录连接失败，请重试。") from error
    if response.status_code in (401, 403):
        raise LLMAuthenticationError(f"{provider} 模型目录认证失败，请检查供应商连接。")
    if response.status_code in (408, 425, 429, 500, 502, 503, 504):
        raise LLMTransientError(f"{provider} 模型目录暂不可用（HTTP {response.status_code}），请重试。")
    if response.status_code != 200:
        raise LLMProviderError(f"读取 {provider} 模型目录失败（HTTP {response.status_code}）。")
    try:
        catalog = _ApiCatalog.model_validate_json(response.content)
    except ValidationError as error:
        raise InvalidLLMResponse("invalid_model_catalog", f"{provider} 模型目录格式不符合约定。") from error
    result = []
    for item in catalog.data:
        if item.shutdown_date is not None and date.fromisoformat(item.shutdown_date) <= date.today():
            continue
        if item.output_modalities is not None and "text" not in item.output_modalities:
            continue
        if item.supported_protocols is not None and "chat" not in item.supported_protocols:
            continue
        if provider == "openai" and not openai_conversation_model(item.id):
            continue
        if provider == "stepfun" and item.id.startswith("stepaudio"):
            continue
        metadata = {"context_window": item.context_window or item.max_input_tokens,
                    "reasoning_levels": [], "default_reasoning_effort": None, "web_search": False}
        if provider == "openai":
            metadata.update(openai_metadata(item.id))
        if item.effort is not None:
            metadata.update(reasoning_levels=item.effort.supported_levels,
                            default_reasoning_effort=item.effort.default_level)
        elif item.reasoning_effort_support_list is not None:
            metadata["reasoning_levels"] = item.reasoning_effort_support_list
        result.append({"model": provider + "/" + item.id, "name": item.name or item.id, **metadata})
    return sorted(result, key=lambda item: item["model"])
