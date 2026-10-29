"""DeepSeek web provider protocol, shared by Web settings and MCP."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit
import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator
from konden.llm.config import load_connections

BASE_URL = "https://api.deepseek.com"


class MissingApiKey(ValueError):
    pass


class DeepSeekAuth(httpx.Auth):
    def __init__(self, connections_path: Path):
        self._path = connections_path

    def auth_flow(self, request):
        key = load_connections(self._path)["deepseek"]["api_key"].strip()
        if not key:
            raise MissingApiKey("请在 connections.json 中填写 deepseek.api_key，或选择其他检索服务。")
        request.headers["Authorization"] = f"Bearer {key}"
        yield request


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: str = Field(min_length=1, max_length=128,
        description="1～128 个字符的搜索词。支持 site:、-site:、引号、lang:、after: 和 before:。")
    count: int = Field(default=10, ge=1, le=50, description="最多返回的结果条数，1～50。")
    ori_question: str | None = Field(default=None, min_length=1, max_length=128,
        description="可选的原始问题，1～128 个字符，用于补充搜索意图。")
    need_content: bool = Field(default=False,
        description="是否同时请求网页正文。默认只返回摘要；正文可能缺失，可按链接调用 web_fetch。")

    @field_validator("query", "ori_question")
    @classmethod
    def nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("不能全为空白")
        return value


class FetchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    url: str = Field(min_length=1, max_length=8192,
        description="要读取的 HTTP/HTTPS 网页地址，最多 8192 个字符，不能携带用户名或密码。")

    @field_validator("url")
    @classmethod
    def http_url(cls, value: str) -> str:
        address = urlsplit(value)
        if (address.scheme not in ("http", "https") or not address.hostname
                or address.username is not None or address.password is not None):
            raise ValueError("必须是含主机名且不携带用户名或密码的 HTTP/HTTPS URL")
        return value


class SearchResult(BaseModel):
    model_config = ConfigDict(strict=True)

    title: str
    url: str
    snippet: str
    summary: str
    content: str
    date: str | None
    site_name: str | None
    site_icon: str | None
    rerank_score: float
    authority: int


class SearchResponse(BaseModel):
    model_config = ConfigDict(strict=True)

    results: list[SearchResult]


class FetchResponse(BaseModel):
    model_config = ConfigDict(strict=True)

    title: str
    content: str


async def request_web(client: httpx.AsyncClient, operation: str, arguments: BaseModel) -> dict:
    response = await client.post(f"/v1/tools/{operation}", json=arguments.model_dump(exclude_none=True))
    response.raise_for_status()
    schema = SearchResponse if operation == "search" else FetchResponse
    result = schema.model_validate(response.json()).model_dump()
    return {"url": arguments.url, **result} if operation == "fetch" else result
