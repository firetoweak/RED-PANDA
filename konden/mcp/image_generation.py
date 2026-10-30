"""Image generation exposed through the ordinary on-demand MCP boundary."""
from __future__ import annotations

import argparse
import asyncio
from base64 import b64encode
from functools import partial
import json
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ImageContent, ListToolsResult, TextContent, Tool, ToolAnnotations
from pydantic import ValidationError

from konden.media.images import ImageGeneration, ImageInput
from konden.filesystem.workspace import EnvironmentInputError
from konden.media.openai_images import ImageProviderError
from konden.model_settings import ModelConfigurationError


async def list_tools(context, params):
    return ListToolsResult(tools=[Tool(
        name="generate_image",
        description="按文字描述生成一张图片，使用用户选择的图片模型与 OpenAI API 连接，按服务用量计费。"
                    "原图保存在当前工作目录的 generated-images/，返回相对路径、尺寸与 PNG 预览。"
                    "预览用于检查；向用户交付原图用 send_file 发送返回的 path，可省略 text。"
                    "仅支持文字生成，不接受参考图片或局部编辑。请求中断后先核对用量，再决定是否重试。",
        inputSchema=ImageInput.model_json_schema(),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True),
    )])


def failure(message):
    return CallToolResult(content=[TextContent(type="text", text=message)], isError=True)


async def call_tool(service: ImageGeneration, output_root: Path | None, context, params):
    if params.name != "generate_image":
        return failure("没有这个工具，请重新查询工具列表。")
    try:
        arguments = ImageInput.model_validate(params.arguments or {})
    except ValidationError as error:
        return failure("；".join(item["msg"] for item in error.errors(include_url=False)))
    if output_root is None:
        return failure("当前连接没有提供工作目录，请在 Bot 对话中生成图片。")
    try:
        image = await service.generate(arguments, output_root)
    except (ModelConfigurationError, ImageProviderError, EnvironmentInputError) as error:
        return failure(str(error))
    result = {"model": image.model, "path": image.path.relative_to(output_root.resolve()).as_posix(),
              "mime": image.mime, "width": image.width, "height": image.height}
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False)),
        ImageContent(type="image", mimeType="image/png", data=b64encode(image.preview).decode("ascii"))],
        structuredContent=result)


async def serve(connections_path: Path, output_root: Path | None):
    service = ImageGeneration(connections_path)
    server = Server("Image Generation", on_list_tools=list_tools,
                    on_call_tool=partial(call_tool, service, output_root))
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options(), raise_exceptions=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connections", type=Path, required=True)
    parser.add_argument("--output-root", type=Path)
    arguments = parser.parse_args()
    asyncio.run(serve(arguments.connections, arguments.output_root))
