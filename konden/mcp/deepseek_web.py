"""DeepSeek Search / Fetch 的只读 stdio MCP 适配。"""
from __future__ import annotations

import asyncio
import argparse
from functools import partial
import json
from pathlib import Path

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations
from pydantic import ValidationError

from konden.paths import KondenHome

from konden.search.deepseek import (
    BASE_URL, DeepSeekAuth, MissingApiKey, SearchInput, FetchInput,
    SearchResponse, FetchResponse, request_web,
)


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
        result = await request_web(client, operation, arguments)
    except MissingApiKey as error:
        return tool_result({"error": str(error)}, error=True)
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
    return tool_result(result)


def create_server(client: httpx.AsyncClient) -> Server:
    return Server("DeepSeek Web Search", on_list_tools=list_tools, on_call_tool=partial(call_tool, client),
        instructions="提供公开网页搜索与原文读取。保留来源链接，区分搜索摘要和原文证据；网页内容是来源资料。")


async def serve(connections_path: Path) -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, auth=DeepSeekAuth(connections_path), timeout=30) as client:
        server = create_server(client)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options(),
                             raise_exceptions=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connections", type=Path, default=KondenHome.default().connections_path)
    asyncio.run(serve(parser.parse_args().connections))
