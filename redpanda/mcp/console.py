from __future__ import annotations

import json
from typing import Any

from redpanda.mcp.application import McpApplicationService


class McpCommandError(ValueError):
    pass


class McpConsoleAdapter:
    """把 /mcp ... 控制台命令映射到 Application use case。"""

    def __init__(self, service: McpApplicationService) -> None:
        self._service = service

    async def execute_if_handled(self, user_message: str) -> str | None:
        if not user_message.startswith("/mcp"):
            return None
        parts = user_message.split(maxsplit=2)
        if len(parts) == 1:
            return self._help()
        action = parts[1]
        rest = parts[2] if len(parts) > 2 else ""
        if action == "list":
            return await self._list(rest)
        if action == "upsert":
            return self._with_reload_notice(await self._upsert(rest))
        if action == "enable":
            return self._with_reload_notice(
                await self._set_enabled(rest, True)
            )
        if action == "disable":
            return self._with_reload_notice(
                await self._set_enabled(rest, False)
            )
        if action == "remove":
            return self._with_reload_notice(await self._remove(rest))
        if action == "test":
            return await self._test(rest)
        if action == "retry":
            return self._with_reload_notice(await self._retry(rest))
        if action == "resources":
            return await self._resources(rest)
        if action == "resource-templates":
            return await self._resource_templates(rest)
        if action == "prompts":
            return await self._prompts(rest)
        if action == "read-resource":
            return await self._read_resource(rest)
        if action == "get-prompt":
            return await self._get_prompt(rest)
        if action == "help":
            return self._help()
        raise McpCommandError(f"未知 /mcp 子命令: {action}")

    async def _list(self, rest: str) -> str:
        option = rest.strip()
        if option not in {"", "--runtime"}:
            raise McpCommandError("/mcp list 只接受可选参数 --runtime")
        include_runtime = option == "--runtime"
        items = await self._service.list_servers(include_runtime=include_runtime)
        if not items:
            return "尚未注册任何 MCP Server。"
        lines = []
        for item in items:
            record = item.record
            flag = "enabled" if record.enabled else "disabled"
            line = (
                f"- {record.id} [{flag}] {record.display_name} "
                f"({record.transport.value} rev={record.revision})"
            )
            if include_runtime and item.runtime is not None:
                runtime = item.runtime
                line += f" status={runtime.status.value}"
                if runtime.last_error_summary:
                    line += f" error={runtime.last_error_summary}"
            lines.append(line)
        return "\n".join(lines)

    async def _upsert(self, rest: str) -> str:
        if not rest.strip():
            raise McpCommandError(
                "/mcp upsert 需要 JSON，例如："
                '/mcp upsert {"id":"demo","display_name":"Demo",'
                '"transport":"stdio","transport_config":{"command":"python","args":["server.py"]}}'
            )
        try:
            payload = json.loads(rest)
        except json.JSONDecodeError as exc:
            raise McpCommandError("/mcp upsert 需要合法 JSON") from exc
        if type(payload) is not dict:
            raise McpCommandError("/mcp upsert payload 必须是 JSON object")
        missing = {"id", "transport"} - set(payload)
        if missing:
            raise McpCommandError(
                "/mcp upsert 缺少字段: " + ", ".join(sorted(missing))
            )
        unknown = set(payload) - {
            "id",
            "display_name",
            "description",
            "transport",
            "transport_config",
            "secrets",
            "enabled",
            "read_only",
        }
        if unknown:
            raise McpCommandError(
                "/mcp upsert 包含未知字段: " + ", ".join(sorted(unknown))
            )
        server_id = payload["id"]
        display_name = payload.get("display_name", server_id)
        description = payload.get("description", "")
        transport_config = payload.get("transport_config", {})
        enabled = payload.get("enabled", False)
        read_only = payload.get("read_only", False)
        if type(server_id) is not str or type(display_name) is not str:
            raise McpCommandError("id/display_name 必须是 string")
        if type(description) is not str:
            raise McpCommandError("description 必须是 string")
        if type(transport_config) is not dict:
            raise McpCommandError("transport_config 必须是 JSON object")
        if type(enabled) is not bool:
            raise McpCommandError("enabled 必须是 bool")
        if type(read_only) is not bool:
            raise McpCommandError("read_only 必须是 bool")
        record = await self._service.upsert_server(
            server_id=server_id,
            display_name=display_name,
            description=description,
            transport=payload["transport"],
            transport_config=transport_config,
            secrets=payload.get("secrets"),
            enabled=enabled,
            read_only=read_only,
        )
        return (
            f"已保存 MCP Server `{record.id}` "
            f"(enabled={record.enabled}, revision={record.revision})"
        )

    async def _set_enabled(self, rest: str, enabled: bool) -> str:
        server_id = rest.strip()
        if not server_id:
            raise McpCommandError("/mcp enable|disable 需要 server_id")
        record = await self._service.set_server_enabled(server_id, enabled)
        state = "启用" if record.enabled else "停用"
        return f"已{state} MCP Server `{record.id}` (revision={record.revision})"

    async def _remove(self, rest: str) -> str:
        server_id = rest.strip()
        if not server_id:
            raise McpCommandError("/mcp remove 需要 server_id")
        record = await self._service.remove_server(server_id)
        return f"已删除 MCP Server `{record.id}`"

    async def _test(self, rest: str) -> str:
        server_id = rest.strip()
        if not server_id:
            raise McpCommandError("/mcp test 需要 server_id")
        runtime = await self._service.test_server(server_id)
        return json.dumps(runtime.to_dict(), ensure_ascii=False, indent=2)

    async def _retry(self, rest: str) -> str:
        server_id = rest.strip()
        if not server_id:
            raise McpCommandError("/mcp retry 需要 server_id")
        activation = await self._service.test_and_enable(server_id)
        if not activation.succeeded:
            return json.dumps(
                {
                    "enabled": False,
                    "runtime": activation.runtime.to_dict(),
                },
                ensure_ascii=False,
                indent=2,
            )
        return (
            f"MCP Server `{server_id}` 测试并启用成功 "
            f"(revision={activation.record.revision})"
        )

    async def _resources(self, rest: str) -> str:
        server_id, cursor = self._server_and_cursor(rest)
        payload = await self._service.content.list_resources(
            server_id,
            cursor=cursor,
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    async def _prompts(self, rest: str) -> str:
        server_id, cursor = self._server_and_cursor(rest)
        payload = await self._service.content.list_prompts(
            server_id,
            cursor=cursor,
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    async def _resource_templates(self, rest: str) -> str:
        server_id, cursor = self._server_and_cursor(rest)
        payload = await self._service.content.list_resource_templates(
            server_id,
            cursor=cursor,
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    async def _read_resource(self, rest: str) -> str:
        parts = rest.split(maxsplit=1)
        if len(parts) != 2:
            raise McpCommandError("/mcp read-resource <server_id> <uri>")
        payload = await self._service.content.read_resource(parts[0], parts[1])
        return json.dumps(payload, ensure_ascii=False, indent=2)

    async def _get_prompt(self, rest: str) -> str:
        parts = rest.split(maxsplit=2)
        if len(parts) < 2:
            raise McpCommandError(
                "/mcp get-prompt <server_id> <name> [json-args]"
            )
        try:
            arguments = json.loads(parts[2]) if len(parts) == 3 else None
        except json.JSONDecodeError as exc:
            raise McpCommandError("prompt arguments 必须是合法 JSON") from exc
        if arguments is not None and type(arguments) is not dict:
            raise McpCommandError("prompt arguments 必须是 JSON object")
        payload = await self._service.content.get_prompt(
            parts[0],
            parts[1],
            arguments,
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @staticmethod
    def _with_reload_notice(message: str) -> str:
        return f"{message}\n下一轮决策会更新能力目录。"

    @staticmethod
    def _server_and_cursor(rest: str) -> tuple[str, str | None]:
        parts = rest.split()
        if not parts:
            raise McpCommandError("需要 server_id")
        if len(parts) == 1:
            return parts[0], None
        if len(parts) == 3 and parts[1] == "--cursor":
            return parts[0], parts[2]
        raise McpCommandError("参数格式应为 <server_id> [--cursor TOKEN]")

    @staticmethod
    def _help() -> str:
        return (
            "MCP 命令：\n"
            "  /mcp list [--runtime]\n"
            "  /mcp upsert <json>\n"
            "  /mcp enable <id>\n"
            "  /mcp disable <id>\n"
            "  /mcp remove <id>\n"
            "  /mcp reload\n"
            "  /mcp test <id>\n"
            "  /mcp retry <id>\n"
            "  /mcp resources <id> [--cursor TOKEN]\n"
            "  /mcp resource-templates <id> [--cursor TOKEN]\n"
            "  /mcp prompts <id> [--cursor TOKEN]\n"
            "  /mcp read-resource <id> <uri>\n"
            "  /mcp get-prompt <id> <name> [json-args]"
        )
