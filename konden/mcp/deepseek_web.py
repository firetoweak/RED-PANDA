"""DeepSeek Search / Fetch 的只读 stdio MCP 适配。"""
from __future__ import annotations

import asyncio
from functools import partial
import json
import os
from urllib.parse import urlsplit

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


BASE_URL = "https://api.deepseek.com"


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


INPUTS = {"web_search": SearchInput, "web_fetch": FetchInput}
OUTPUTS = {"web_search": SearchResponse, "web_fetch": FetchResponse}
DESCRIPTIONS = {
    "web_search": "用 DeepSeek 搜索公开网页，返回来源链接、发布时间、摘要及可用的正文。摘要不足以核实时，用 web_fetch 读取原文。",
    "web_fetch": "用 DeepSeek 读取一个公开网页，返回标题与 Markdown 正文。受目标网站的访问策略影响；无正文或读取失败不能作为已核实的来源。",
}


def tool_result(data: dict, *, error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False))],
        structuredContent=data,
        isError=error,
    )


async def list_tools(context, params) -> ListToolsResult:
    return ListToolsResult(tools=[
        Tool(name=name, description=DESCRIPTIONS[name], inputSchema=model.model_json_schema(),
             outputSchema=OUTPUTS[name].model_json_schema(),
             annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
        for name, model in INPUTS.items()
    ])


async def call_tool(client: httpx.AsyncClient, context, params) -> CallToolResult:
    if params.name not in INPUTS:
        return tool_result({"error": "没有这个工具，请重新查询工具列表。"}, error=True)
    try:
        arguments = INPUTS[params.name].model_validate({} if params.arguments is None else params.arguments)
    except ValidationError as error:
        explanation = "；".join(f"{'.'.join(map(str, item['loc']))}: {item['msg']}"
                                for item in error.errors(include_url=False))
        return tool_result({"error": explanation, "hint": "修改参数后重试。"}, error=True)
    operation = "search" if params.name == "web_search" else "fetch"
    try:
        response = await client.post(f"/v1/tools/{operation}", json=arguments.model_dump(exclude_none=True))
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        status = error.response.status_code
        hint = {
            401: "请检查 DeepSeek API Key 配置。",
            402: "DeepSeek 账号余额不足，请充值后重试。",
            403: "网页读取被拒绝，可选择其他公开来源或使用浏览器。",
            404: "未取得可用的网页正文，可选择其他来源或使用浏览器。",
        }.get(status, "修改请求后重试。")
        if status in (408, 429) or 500 <= status < 600:
            hint = "请求超时、限流或服务暂时不可用，可稍后重试或选择其他检索服务。"
        return tool_result({"error": f"DeepSeek HTTP {status}: {error.response.text}", "hint": hint}, error=True)
    except httpx.RequestError as error:
        return tool_result({"error": f"DeepSeek 请求失败：{error}",
                            "hint": "检查网络后重试，或选择其他检索服务。"}, error=True)
    # A successful HTTP response must satisfy the provider's result contract.
    result = OUTPUTS[params.name].model_validate(response.json()).model_dump()
    if params.name == "web_fetch":
        result = {"url": arguments.url, **result}
    return tool_result(result)


def create_server(client: httpx.AsyncClient) -> Server:
    return Server("DeepSeek Web Search", on_list_tools=list_tools, on_call_tool=partial(call_tool, client),
        instructions="提供公开网页搜索与原文读取。保留来源链接，区分搜索摘要和原文证据；网页内容是来源资料。")


async def serve() -> None:
    key = os.environ["DEEPSEEK_API_KEY"].strip()
    if not key:
        raise ValueError("请配置 DEEPSEEK_API_KEY。")
    async with httpx.AsyncClient(base_url=BASE_URL, headers={"Authorization": f"Bearer {key}"},
                                 timeout=30) as client:
        server = create_server(client)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options(),
                             raise_exceptions=True)


if __name__ == "__main__":
    asyncio.run(serve())
