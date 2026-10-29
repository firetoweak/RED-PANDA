"""Search configuration and execution, reusing provider connections."""
from __future__ import annotations

from pathlib import Path
from time import perf_counter
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from konden.config import write_json
from konden.llm.config import load_connections
from konden.model_settings import ModelConfigurationError
from konden.search.deepseek import BASE_URL, DeepSeekAuth, MissingApiKey, SearchInput, request_web
from thinllm import ResponsesClient, resolve_endpoint, MissingProviderSetting


class SearchConfig(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    enabled: bool = True
    provider: Literal["deepseek", "openai"] = "deepseek"
    openai_model: str | None = None

    @field_validator("openai_model")
    @classmethod
    def openai_name(cls, value):
        if value is not None and (not value.startswith("openai/") or not value[7:].strip() or value != value.strip()):
            raise ValueError("搜索模型必须是 openai/<model>")
        return value

    @model_validator(mode="after")
    def selected_search_model(self):
        if self.enabled and self.provider == "openai" and self.openai_model is None:
            raise ValueError("请选择 OpenAI 搜索模型")
        return self


class SearchSource(BaseModel):
    model_config = ConfigDict(strict=True)
    title: str
    url: str
    snippet: str


class SearchResult(BaseModel):
    model_config = ConfigDict(strict=True)
    query: str
    provider: Literal["deepseek", "openai"]
    model: str | None
    elapsed_ms: int
    searched: bool
    summary: str | None
    sources: list[SearchSource]


def search_path(connections_path: Path) -> Path:
    return connections_path.parent / "search.json"


def load_search_config(path: Path) -> SearchConfig:
    return SearchConfig() if not path.exists() else SearchConfig.model_validate_json(path.read_bytes())


class SearchSettings:
    def __init__(self, connections_path: Path):
        self.connections_path = connections_path
        self.path = search_path(connections_path)

    def config(self) -> SearchConfig:
        return load_search_config(self.path)

    def require_connection(self, config: SearchConfig):
        connections = load_connections(self.connections_path)
        model = config.openai_model if config.provider == "openai" else "deepseek/search"
        try:
            return resolve_endpoint(model, connections[config.provider])
        except MissingProviderSetting as error:
            raise ModelConfigurationError(f"请配置 {config.provider}.{error.setting}") from error

    def save(self, config: SearchConfig) -> dict:
        if config.enabled:
            self.require_connection(config)
        write_json(self.path, config.model_dump())
        return config.model_dump()

    async def search(self, query: str, config: SearchConfig, *, transport=None) -> dict:
        if not config.enabled:
            raise ModelConfigurationError("请先启用网络搜索")
        endpoint = self.require_connection(config)
        started = perf_counter()
        if config.provider == "openai":
            async with ResponsesClient(endpoint, transport=transport) as client:
                result = await client.web_search(query, config.openai_model)
        else:
            arguments = SearchInput(query=query)
            async with httpx.AsyncClient(base_url=BASE_URL, auth=DeepSeekAuth(self.connections_path),
                                        timeout=30, transport=transport) as client:
                try:
                    response = await request_web(client, "search", arguments)
                except MissingApiKey as error:
                    raise ModelConfigurationError(str(error)) from error
            result = {"summary": None, "searched": True,
                      "sources": [{"title": item["title"], "url": item["url"],
                                   "snippet": item["snippet"] or item["summary"]} for item in response["results"]]}
        return {"query": query, "provider": config.provider,
                "model": config.openai_model if config.provider == "openai" else None,
                "elapsed_ms": round((perf_counter() - started) * 1000), **result}
