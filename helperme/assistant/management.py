"""MCP / Skill 管理能力的渐进式模型表面。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from helperme.assistant.artifacts import ArtifactGateway
from helperme.assistant.context.projection import (
    ModelContextSettings,
    externalize_tool_result,
)
from helperme.runtime import (
    CommandOutcomeReceived,
    Event,
    InvokeTool,
    OutcomeStatus,
    StepCommitted,
    ToolBinding,
)
from helperme.runtime.dispatcher import AttemptContext
from helperme.runtime.model import DecisionState
from helperme.tools.control import ControlOperation
from helperme.tools.spec import ToolArgumentsError, ToolSpec


LOAD_MANAGEMENT_TOOLS = "load_management_tools"


@dataclass(frozen=True, slots=True)
class ResidentTool:
    """常驻工具的归属声明。Schema 与绑定留在各自暴露处，这里只认名字。"""

    name: str
    description: str

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name:
            raise ValueError("resident tool name must be a non-empty str")
        if type(self.description) is not str or not self.description:
            raise ValueError("resident tool description must be a non-empty str")


@dataclass(frozen=True, slots=True)
class ManagementDomain:
    id: str
    description: str
    diagnostic_specs: tuple[ToolSpec, ...]
    control_operations: tuple[ControlOperation, ...]
    resident_tools: tuple[ResidentTool, ...]

    def __post_init__(self) -> None:
        if type(self.id) is not str or not self.id:
            raise ValueError("management domain id must be a non-empty str")
        if type(self.description) is not str or not self.description:
            raise ValueError("management domain description must be a non-empty str")
        if type(self.diagnostic_specs) is not tuple:
            raise TypeError("management diagnostic specs must be tuple")
        if type(self.control_operations) is not tuple:
            raise TypeError("management control operations must be tuple")
        if type(self.resident_tools) is not tuple:
            raise TypeError("management resident tools must be tuple")
        if any(operation.domain != self.id for operation in self.control_operations):
            raise ValueError("control operation domain does not match management domain")

    @property
    def control_names(self) -> tuple[str, ...]:
        return tuple(operation.name for operation in self.control_operations)

    @property
    def resident_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.resident_tools)


@dataclass(frozen=True, slots=True)
class ManagementActivation:
    domain: str
    command_id: str


def project_management_activations(
    events: Sequence[Event],
) -> tuple[ManagementActivation, ...]:
    load_commands: set[str] = set()
    for event in events:
        payload = event.payload
        if not isinstance(payload, StepCommitted):
            continue
        for command in payload.step.commands:
            effect = command.effect
            if isinstance(effect, InvokeTool) and effect.name == LOAD_MANAGEMENT_TOOLS:
                load_commands.add(command.command_id)

    activations: list[ManagementActivation] = []
    for event in events:
        payload = event.payload
        if (
            not isinstance(payload, CommandOutcomeReceived)
            or payload.command_id not in load_commands
            or payload.outcome.status is not OutcomeStatus.SUCCEEDED
        ):
            continue
        value = payload.outcome.value
        if not isinstance(value, Mapping):
            raise ValueError("load_management_tools outcome 必须是 object")
        if "ok" not in value or type(value["ok"]) is not bool:
            raise ValueError("load_management_tools outcome ok 无效")
        if value["ok"] is False:
            continue
        if set(value) != {"ok", "code", "data"}:
            raise ValueError("load_management_tools outcome 字段不匹配")
        data = value["data"]
        if (
            value["ok"] is not True
            or value["code"] != "MANAGEMENT_TOOLS_LOADED"
            or not isinstance(data, Mapping)
        ):
            raise ValueError("load_management_tools outcome 不符合成功契约")
        if set(data) != {"domain", "tools"}:
            raise ValueError("load_management_tools outcome data 字段不匹配")
        domain = data["domain"]
        tools = data["tools"]
        if type(domain) is not str or not domain:
            raise ValueError("load_management_tools domain 无效")
        if not isinstance(tools, tuple) or any(
            not isinstance(tool, Mapping)
            or set(tool) != {"name", "kind", "description"}
            or type(tool["name"]) is not str
            or type(tool["description"]) is not str
            or tool["kind"] not in {"diagnostic", "control", "resident"}
            for tool in tools
        ):
            raise ValueError("load_management_tools tools 无效")
        activations.append(ManagementActivation(domain, payload.command_id))
    return tuple(activations)


class ManagementToolAdapter:
    """只负责 ToolSpec 到 Runtime Binding 的边界适配。"""

    def __init__(
        self,
        specs: Sequence[ToolSpec],
        gateway: ArtifactGateway,
        settings: ModelContextSettings,
    ) -> None:
        self._specs = {spec.name: spec for spec in specs}
        if len(self._specs) != len(specs):
            raise ValueError("管理诊断工具名称重复")
        if any(spec.control_boundary for spec in specs):
            raise ValueError("诊断工具不能跨越控制审批边界")
        self._gateway = gateway
        self._settings = settings

    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def schema(self, name: str) -> dict[str, object]:
        return self._specs[name].to_openai_tool()

    def bindings(self) -> dict[str, ToolBinding]:
        return {
            name: ToolBinding(self._handler(spec))
            for name, spec in self._specs.items()
        }

    def _handler(self, spec: ToolSpec):
        async def handler(
            context: AttemptContext,
            arguments: Mapping[str, object],
        ) -> object:
            try:
                input_data = spec.parameters.validate(dict(arguments))
            except ToolArgumentsError as exc:
                return {
                    "ok": False,
                    "code": "VALIDATION_ERROR",
                    "data": {"details": exc.details},
                    "error": "management tool arguments validation failed",
                    "hint": "按本次决策提供的 schema 修正参数。",
                }
            result = await spec.handler(input_data)
            if type(result) is not dict:
                raise TypeError("管理诊断工具返回值不符合契约")
            return externalize_tool_result(
                result,
                context.session_id,
                self._gateway,
                self._settings,
            )

        return handler


class ManagementSurface:
    """常驻管理目录；具体诊断与控制 schema 按 Session 激活。"""

    def __init__(
        self,
        domains: Sequence[ManagementDomain],
        gateway: ArtifactGateway,
        settings: ModelContextSettings,
    ) -> None:
        self._domains = {domain.id: domain for domain in domains}
        if len(self._domains) != len(domains):
            raise ValueError("管理域 ID 重复")
        specs = tuple(spec for domain in domains for spec in domain.diagnostic_specs)
        self._adapter = ManagementToolAdapter(specs, gateway, settings)
        control_names = tuple(name for domain in domains for name in domain.control_names)
        if len(control_names) != len(set(control_names)):
            raise ValueError("跨管理域控制工具名称重复")
        resident_names = tuple(name for domain in domains for name in domain.resident_names)
        if len(resident_names) != len(set(resident_names)):
            raise ValueError("跨管理域常驻工具名称重复")
        self._loaded: dict[str, dict[str, set[str]]] = {}

    def names(self) -> tuple[str, ...]:
        return (LOAD_MANAGEMENT_TOOLS, *self._adapter.names())

    def resident_names(self) -> tuple[str, ...]:
        return tuple(
            name
            for domain in self._domains.values()
            for name in domain.resident_names
        )

    def bindings(self) -> dict[str, ToolBinding]:
        return {
            LOAD_MANAGEMENT_TOOLS: ToolBinding(self._load_handler),
            **self._adapter.bindings(),
        }

    def schemas(
        self,
        session_id: str,
        decision_state: DecisionState | None = None,
    ) -> list[dict[str, object]]:
        schemas = [self._loader_schema()]
        for domain_id in self._visible_domains(session_id, decision_state):
            schemas.extend(
                self._adapter.schema(spec.name)
                for spec in self._domains[domain_id].diagnostic_specs
            )
        return schemas

    def control_names(
        self,
        session_id: str,
        decision_state: DecisionState | None = None,
    ) -> frozenset[str]:
        return frozenset(
            name
            for domain_id in self._visible_domains(session_id, decision_state)
            for name in self._domains[domain_id].control_names
        )

    def catalog_instruction(
        self,
        session_id: str,
        decision_state: DecisionState | None = None,
    ) -> str:
        lines = [
            "管理能力按需加载。需要诊断、安装、更新或修复时，先调用 "
            "load_management_tools；具体工具从下一次决策开始可用："
        ]
        for domain in self._domains.values():
            line = f"- {domain.id}: {domain.description}"
            if domain.resident_tools:
                line += f"（以下工具不必加载，已可直接调用：{'、'.join(domain.resident_names)}）"
            lines.append(line)
        return "\n".join(lines)

    async def load(
        self,
        session_id: str,
        domain_id: object,
        *,
        activation_command_id: str,
    ) -> dict[str, object]:
        if type(domain_id) is not str or not domain_id:
            return {
                "ok": False,
                "code": "INVALID_ARGUMENT",
                "data": {"domain": domain_id},
                "error": "domain 必须是非空字符串",
            }
        domain = self._domains.get(domain_id)
        if domain is None:
            return {
                "ok": False,
                "code": "MANAGEMENT_DOMAIN_NOT_FOUND",
                "data": {"domain": domain_id},
                "error": f"没有名为 {domain_id} 的管理类别",
                "hint": "请从管理能力目录中选择有效 domain。",
            }
        self._loaded.setdefault(session_id, {}).setdefault(domain_id, set()).add(
            activation_command_id
        )
        return self._loaded_payload(domain)

    async def rehydrate(
        self,
        session_id: str,
        events: Sequence[Event],
    ) -> tuple[ManagementActivation, ...]:
        activations = project_management_activations(events)
        restored: dict[str, set[str]] = {}
        for activation in activations:
            if activation.domain not in self._domains:
                raise ValueError(f"Management domain {activation.domain} is unavailable")
            restored.setdefault(activation.domain, set()).add(activation.command_id)
        if restored:
            self._loaded[session_id] = restored
        else:
            self._loaded.pop(session_id, None)
        return activations

    async def _load_handler(
        self,
        context: AttemptContext,
        arguments: Mapping[str, object],
    ) -> object:
        return await self.load(
            context.session_id,
            arguments.get("domain"),
            activation_command_id=context.command_id,
        )

    def _loader_schema(self) -> dict[str, object]:
        return {
            "type": "function",
            "function": {
                "name": LOAD_MANAGEMENT_TOOLS,
                "description": "按需加载一类管理诊断与控制工具。具体工具从下一次决策开始可用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "domain": {
                            "type": "string",
                            "enum": list(self._domains),
                            "description": "要加载的管理类别",
                        }
                    },
                    "required": ["domain"],
                    "additionalProperties": False,
                },
            },
        }

    def _visible_domains(
        self,
        session_id: str,
        decision_state: DecisionState | None,
    ) -> frozenset[str]:
        loaded = self._loaded.get(session_id, {})
        if decision_state is None:
            return frozenset(loaded)
        visible: set[str] = set()
        for domain_id, command_ids in loaded.items():
            for command_id in command_ids:
                command = decision_state.command(command_id)
                if command.outcome is not None and command.outcome.status is OutcomeStatus.SUCCEEDED:
                    visible.add(domain_id)
                    break
        return frozenset(visible)

    @staticmethod
    def _loaded_payload(domain: ManagementDomain) -> dict[str, object]:
        return {
            "ok": True,
            "code": "MANAGEMENT_TOOLS_LOADED",
            "data": {
                "domain": domain.id,
                "tools": [
                    *(
                        {"name": spec.name, "kind": "diagnostic", "description": spec.description}
                        for spec in domain.diagnostic_specs
                    ),
                    *(
                        {"name": operation.name, "kind": "control",
                         "description": operation.proposal_spec.description}
                        for operation in domain.control_operations
                    ),
                    *(
                        {"name": item.name, "kind": "resident",
                         "description": item.description}
                        for item in domain.resident_tools
                    ),
                ],
            },
        }
