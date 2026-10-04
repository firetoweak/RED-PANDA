import json
from collections.abc import Mapping
from typing import Any

from redpanda.tools.control import ControlApprovalProposal, ControlPreparationFailure
from redpanda.tools.registry import ToolRegistry
from redpanda.tools.spec import ToolArgumentsError, ToolSpec


RESERVED_KEYS = frozenset({"ok", "code", "data", "error", "hint"})


def _mutable_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _mutable_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mutable_json(item) for item in value]
    return value


def _as_str_error(error: Any) -> str | None:
    if error is None:
        return None
    if isinstance(error, str):
        return error
    return json.dumps(error, ensure_ascii=False)


def normalize_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    extra = {k: v for k, v in result.items() if k not in RESERVED_KEYS}
    return {
        "ok": result["ok"],
        "code": result["code"],
        "data": result["data"] if "data" in result else extra or None,
        "error": _as_str_error(result.get("error")),
        "hint": result.get("hint"),
    }


def _tool_not_found(tool_name: str) -> dict[str, Any]:
    return normalize_tool_result(
        {
            "ok": False,
            "code": "TOOL_NOT_FOUND",
            "data": {"tool_name": tool_name},
            "error": f"工具 {tool_name} 当前不可用",
            "hint": (
                "只能调用本次决策中暴露的精确名称；"
                "能力来自未加载的 Toolset 时，先调用 load_toolset。"
            ),
        }
    )


class ToolsExecutor:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def execute(
        self,
        tool_name: str,
        tool_arguments: str,
    ) -> dict[str, Any] | ControlApprovalProposal | ControlPreparationFailure:
        spec = self.registry.get(tool_name)
        if spec is None:
            return _tool_not_found(tool_name)

        if not tool_arguments or not tool_arguments.strip():
            return normalize_tool_result(
                {
                    "ok": False,
                    "code": "INVALID_JSON",
                    "error": "tool arguments 不能为空；无参工具也必须显式传入 {}",
                    "hint": "传入合法的 JSON object。",
                }
            )

        try:
            payload = json.loads(tool_arguments)
        except json.JSONDecodeError as exc:
            return normalize_tool_result(
                {
                    "ok": False,
                    "code": "INVALID_JSON",
                    "error": f"invalid json: {exc}",
                    "hint": "修正工具 arguments 的 JSON 格式后重试。",
                }
            )

        if not isinstance(payload, dict):
            return normalize_tool_result(
                {
                    "ok": False,
                    "code": "VALIDATION_ERROR",
                    "error": "tool arguments 必须是 JSON object",
                    "hint": "按工具 schema 修正参数后重试。",
                }
            )
        return await self._execute_spec(spec, payload)

    async def execute_parsed(
        self,
        tool_name: str,
        tool_arguments: Mapping[str, object],
    ) -> dict[str, Any] | ControlApprovalProposal | ControlPreparationFailure:
        """执行已经通过模型协议边界解析的内部参数。"""

        spec = self.registry.get(tool_name)
        if spec is None:
            raise KeyError(tool_name)
        payload = _mutable_json(tool_arguments)
        return await self._execute_spec(spec, payload)

    @staticmethod
    async def _execute_spec(
        spec: ToolSpec,
        payload: object,
    ) -> dict[str, Any] | ControlApprovalProposal | ControlPreparationFailure:
        try:
            data = spec.parameters.validate(payload)
        except ToolArgumentsError as exc:
            return normalize_tool_result(
                {
                    "ok": False,
                    "code": "VALIDATION_ERROR",
                    "error": exc.details,
                    "hint": "按工具 schema 修正参数后重试。",
                }
            )

        result = await spec.handler(data)
        if isinstance(result, (ControlApprovalProposal, ControlPreparationFailure)):
            return result
        return normalize_tool_result(result)
