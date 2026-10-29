"""The configured OpenAI search model exposed as a read-only MCP tool."""
from __future__ import annotations

import argparse
import asyncio
from functools import partial
import json
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from konden.model_settings import ModelConfigurationError
from konden.paths import KondenHome
from konden.search import SearchResult, SearchSettings
from thinllm import InvalidLLMResponse, LLMProviderError, LLMTransientError


class SearchInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    query: str = Field(min_length=1, max_length=4000, description="要交给搜索模型回答的问题，最多 4000 个字符。")

    @field_validator("query")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("问题不能为空白")
        return value


async def list_tools(context, params):
    return ListToolsResult(tools=[Tool(
        name="web_search",
        description="使用已选择的 OpenAI 搜索模型回答问题。它自行决定是否联网；searched 表示是否实际检索。返回回答与来源链接，回答不是网页原文。",
        inputSchema=SearchInput.model_json_schema(), outputSchema=SearchResult.model_json_schema(),
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )])


def tool_result(data, *, error=False):
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False))],
                          structuredContent=data, isError=error)


async def call_tool(settings: SearchSettings, context, params):
    if params.name != "web_search":
        return tool_result({"error": "没有这个工具，请重新查询工具列表。"}, error=True)
    try:
        arguments = SearchInput.model_validate(params.arguments or {})
    except ValidationError as error:
        explanation = "；".join(item["msg"] for item in error.errors(include_url=False))
        return tool_result({"error": explanation, "hint": "修改问题后重试。"}, error=True)
    config = settings.config()
    if not config.enabled or config.provider != "openai":
        return tool_result({"error": "OpenAI 搜索已停用，请重新查看可选工具。"}, error=True)
    try:
        return tool_result(await settings.search(arguments.query, config))
    except (ModelConfigurationError, LLMProviderError, LLMTransientError, InvalidLLMResponse) as error:
        return tool_result({"error": str(error), "hint": "检查网络搜索配置后重试，或使用浏览器核实来源。"}, error=True)


async def serve(connections_path: Path):
    settings = SearchSettings(connections_path)
    server = Server("OpenAI Web Search", on_list_tools=list_tools, on_call_tool=partial(call_tool, settings),
                    instructions="回答来自搜索模型；保留来源链接。searched=false 表示本次没有联网检索，不能当作已核实的最新信息。")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options(), raise_exceptions=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connections", type=Path, default=KondenHome.default().connections_path)
    asyncio.run(serve(parser.parse_args().connections))
