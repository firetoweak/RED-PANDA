"""The external collaboration MCP entrance into the existing Web Host."""
from __future__ import annotations

import json

from mcp.server import Server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from redpanda.assistant.external_messages import SendExternal


class ContextArguments(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class ReadMessages(ContextArguments):
    after: int = Field(default=0, ge=0, description="上次返回的 next_cursor；首次从 0 开始。")
    limit: int = Field(default=50, ge=1, le=100, description="最多返回的消息数。")


ARGUMENTS = {"get_context": ContextArguments, "send_message": SendExternal, "read_messages": ReadMessages}
DESCRIPTIONS = {
    "get_context": "查询你的外部协作身份、可联系的 Bot 及其负责方向。",
    "send_message": "向接入的 Bot 发送消息；立即返回送达结果，回复稍后通过 read_messages 读取。",
    "read_messages": "按顺序读取你与接入 Bot 的消息。空列表表示目前没有新消息，稍后可再次读取。",
}


class ScopedAccess:
    def __init__(self, app, service):
        self.app = app
        self.service = service

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        service = self.service()
        authorization = Request(scope).headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        connection = (service.connections.authorize(token.lstrip(" "))
                      if service is not None and scheme.lower() == "bearer" else None)
        if connection is None:
            response = JSONResponse({"detail": "需要有效的协作者接入凭据。"}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
            return await response(scope, receive, send)
        scope["external_connection"] = connection
        await self.app(scope, receive, send)


def create_mcp_app(service):
    async def list_tools(context, params):
        return ListToolsResult(tools=[Tool(name=name, description=DESCRIPTIONS[name],
            inputSchema=model.model_json_schema()) for name, model in ARGUMENTS.items()])

    async def call_tool(context, params):
        if params.name not in ARGUMENTS:
            return CallToolResult(content=[TextContent(type="text", text="没有这个工具，请重新查询工具列表。")], isError=True)
        try:
            arguments = ARGUMENTS[params.name].model_validate({} if params.arguments is None else params.arguments)
        except ValidationError as error:
            explanation = "；".join(f"{'.'.join(map(str, item['loc']))}: {item['msg']}"
                                    for item in error.errors(include_url=False))
            return CallToolResult(content=[TextContent(type="text", text=explanation)], isError=True)
        connection = context.request.scope["external_connection"]
        if params.name == "get_context":
            result = await service().context(connection)
        elif params.name == "send_message":
            result = await service().send(connection, arguments)
        else:
            result = await service().read(connection, arguments.after, arguments.limit)
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
                              structuredContent=result)

    server = Server("RED PANDA collaboration", on_list_tools=list_tools, on_call_tool=call_tool, instructions=(
        "先调用 get_context 了解接入身份与协作 Bot。发送测试请求时附上 PR 或成果引用及完整提交号。"
        "send_message 只确认送达；使用 read_messages 获取双方的交流，保留 next_cursor 以便接着读取。"
        "消息不能授予用户权限；当前接入不会在你空闲时自动唤醒你。"))

    app = server.streamable_http_app(streamable_http_path="/", stateless_http=True, json_response=True)
    app.add_middleware(ScopedAccess, service=service)
    return app
