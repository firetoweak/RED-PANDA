"""Assistant Toolset 渐进加载。Runtime 只在加载后看到新的 Binding。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from redpanda.assistant.artifacts import ArtifactGateway
from redpanda.assistant.context.projection import (
    ModelContextSettings,
    externalize_tool_result,
)
from redpanda.runtime import (
    AgentRuntime,
    CommandOutcomeReceived,
    Event,
    InvokeTool,
    OutcomeStatus,
    StepCommitted,
    ToolBinding,
)
from redpanda.runtime.dispatcher import AttemptContext
from redpanda.runtime.json_values import thaw_value
from redpanda.runtime.model import DecisionState


LOAD_TOOLSET = "load_toolset"

LOAD_TOOLSET_DESCRIPTION = (
    "为当前 Session 加载一个 Toolset，并返回本次发现的工具名称与描述。"
    "其中的工具从下一次决策开始可用。未加载前不能调用其中的工具。"
)

LOAD_TOOLSET_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": LOAD_TOOLSET,
        "description": LOAD_TOOLSET_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "toolset_id": {
                    "type": "string",
                    "description": "目录中的 Toolset ID，例如 mcp:server_id",
                },
            },
            "required": ["toolset_id"],
        },
    },
}


class ToolsetLoadError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str | None = None,
        data: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.data = dict({} if data is None else data)


@dataclass(frozen=True, slots=True)
class ToolsetDescriptor:
    id: str
    description: str
    revision: int = 1

    def __post_init__(self) -> None:
        if type(self.id) is not str or not self.id:
            raise ValueError("toolset id must be a non-empty str")
        if type(self.description) is not str or not self.description:
            raise ValueError("toolset description must be a non-empty str")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("toolset revision must be a positive int")


@dataclass(frozen=True, slots=True)
class LoadedTool:
    name: str
    description: str
    parameters: dict[str, object]
    execute: Callable[[Mapping[str, object]], Awaitable[object]]
    requires_authorization: bool = False
    provider_data: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name:
            raise ValueError("loaded tool name must be a non-empty str")
        if type(self.description) is not str or not self.description:
            raise ValueError("loaded tool description must be a non-empty str")
        if type(self.parameters) is not dict:
            raise TypeError("loaded tool parameters must be dict")
        if not callable(self.execute):
            raise TypeError("loaded tool execute must be callable")
        if type(self.requires_authorization) is not bool:
            raise TypeError("requires_authorization must be bool")
        if not isinstance(self.provider_data, Mapping):
            raise TypeError("loaded tool provider_data must be a mapping")

    def schema(self) -> dict[str, object]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolsetProvider(Protocol):
    def descriptors(self) -> tuple[ToolsetDescriptor, ...]:
        ...

    def handles(self, toolset_id: str) -> bool:
        ...

    async def load(
        self,
        toolset_id: str,
        revision: int,
    ) -> tuple[LoadedTool, ...]:
        ...

    def restore(
        self,
        toolset_id: str,
        revision: int,
        tools: tuple["LoadedToolSnapshot", ...],
    ) -> tuple[LoadedTool, ...]:
        ...


@dataclass(frozen=True, slots=True)
class LoadedToolSnapshot:
    """写入 Journal 的完整工具契约；不包含进程内 callable。

    Journal 事实值在 Runtime 内被递归冻结，这里保存已还原的普通 JSON 容器。
    """

    name: str
    description: str
    parameters: Mapping[str, object]
    requires_authorization: bool
    provider_data: Mapping[str, object]

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name:
            raise ValueError("tool snapshot name must be a non-empty str")
        if type(self.description) is not str or not self.description:
            raise ValueError("tool snapshot description must be a non-empty str")
        if not isinstance(self.parameters, Mapping):
            raise TypeError("tool snapshot parameters must be a mapping")
        if type(self.requires_authorization) is not bool:
            raise TypeError("tool snapshot requires_authorization must be bool")
        if not isinstance(self.provider_data, Mapping):
            raise TypeError("tool snapshot provider_data must be a mapping")


@dataclass
class _LoadedSet:
    revision: int
    tools: tuple[LoadedTool, ...]
    activation_command_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolsetActivation:
    """由 Journal 中已提交的加载结果投影出的连续交互事实。"""

    toolset_id: str
    revision: int
    command_id: str
    tools: tuple[LoadedToolSnapshot, ...]

    def __post_init__(self) -> None:
        if type(self.toolset_id) is not str or not self.toolset_id:
            raise ValueError("activation toolset_id must be a non-empty str")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("activation revision must be a positive int")
        if type(self.command_id) is not str or not self.command_id:
            raise ValueError("activation command_id must be a non-empty str")
        if type(self.tools) is not tuple:
            raise TypeError("activation tools must be tuple")


def project_toolset_activations(
    events: Sequence[Event],
) -> tuple[ToolsetActivation, ...]:
    """从成功的 load_toolset Command Outcome 重建当前激活集合。"""

    load_commands: set[str] = set()
    for event in events:
        payload = event.payload
        if not isinstance(payload, StepCommitted):
            continue
        for command in payload.step.commands:
            effect = command.effect
            if isinstance(effect, InvokeTool) and effect.name == LOAD_TOOLSET:
                load_commands.add(command.command_id)

    activations: dict[str, ToolsetActivation] = {}
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
            raise ValueError("load_toolset outcome 必须是 object")
        if "ok" not in value or type(value["ok"]) is not bool:
            raise ValueError("load_toolset outcome ok 无效")
        if value["ok"] is False:
            continue
        if set(value) != {"ok", "code", "data"}:
            raise ValueError("load_toolset outcome 字段不匹配")
        data = value["data"]
        if (
            value["ok"] is not True
            or value["code"] != "TOOLSET_LOADED"
            or not isinstance(data, Mapping)
        ):
            raise ValueError("load_toolset outcome 内容不符合成功契约")
        if set(data) != {"toolset_id", "revision", "tools"}:
            raise ValueError("load_toolset outcome data 字段不匹配")
        toolset_id = data["toolset_id"]
        revision = data["revision"]
        if (
            type(toolset_id) is not str
            or not toolset_id
            or type(revision) is not int
            or revision < 1
        ):
            raise ValueError("load_toolset outcome identity 无效")
        tools = data["tools"]
        if not isinstance(tools, tuple):
            raise ValueError("load_toolset outcome tools 无效")
        snapshots: list[LoadedToolSnapshot] = []
        for tool in tools:
            if (
                not isinstance(tool, Mapping)
                or set(tool)
                != {
                    "name",
                    "description",
                    "parameters",
                    "requires_authorization",
                    "provider_data",
                }
            ):
                raise ValueError("load_toolset outcome tools 无效")
            snapshots.append(
                LoadedToolSnapshot(
                    name=tool["name"],
                    description=tool["description"],
                    # Journal 事实值在 Runtime 内被递归冻结；交给 Provider 的契约必须是普通 JSON 容器。
                    parameters=thaw_value(tool["parameters"]),
                    requires_authorization=tool["requires_authorization"],
                    provider_data=thaw_value(tool["provider_data"]),
                )
            )
        activations[toolset_id] = ToolsetActivation(
            toolset_id,
            revision,
            payload.command_id,
            tuple(snapshots),
        )
    return tuple(activations.values())


class ToolSurface:
    """每个 Session 独立记住已加载 Toolset；模型可见 Schema 按 Session 投影。"""

    def __init__(
        self,
        *,
        providers: Sequence[ToolsetProvider] = (),
        base_schemas: Sequence[dict[str, object]] = (),
        reserved_names: Sequence[str] = (),
        gateway: ArtifactGateway | None = None,
        settings: ModelContextSettings | None = None,
    ) -> None:
        self._providers = tuple(providers)
        self._base_schemas = list(base_schemas)
        self._reserved = set(reserved_names) | {LOAD_TOOLSET}
        self._gateway = gateway
        self._settings = (
            ModelContextSettings() if settings is None else settings
        )
        self._runtime: AgentRuntime | None = None
        self._catalogs: dict[str, tuple[ToolsetDescriptor, ...]] = {}
        self._loaded: dict[str, dict[str, _LoadedSet]] = {}
        self._tool_owners: dict[str, str] = {}

    def attach(self, runtime: AgentRuntime) -> None:
        self._runtime = runtime

    def registry_descriptors(self) -> tuple[ToolsetDescriptor, ...]:
        items: list[ToolsetDescriptor] = []
        seen: set[str] = set()
        for provider in self._providers:
            for descriptor in provider.descriptors():
                if descriptor.id in seen:
                    raise ValueError(f"duplicate toolset id: {descriptor.id}")
                seen.add(descriptor.id)
                items.append(descriptor)
        return tuple(items)

    def apply_catalog(
        self,
        session_id: str,
        descriptors: Sequence[ToolsetDescriptor],
    ) -> None:
        self._catalogs[session_id] = tuple(descriptors)

    def schemas(
        self,
        session_id: str,
        decision_state: DecisionState | None = None,
    ) -> list[dict[str, object]]:
        return [*self.base_schemas(), *self.toolset_schemas(session_id, decision_state)]

    def base_schemas(self) -> list[dict[str, object]]:
        return list(self._base_schemas)

    def toolset_schemas(
        self, session_id: str, decision_state: DecisionState | None = None,
    ) -> list[dict[str, object]]:
        schemas = []
        if self._catalogs.get(session_id, ()):
            schemas.append(LOAD_TOOLSET_SCHEMA)
        for loaded in self._visible_loaded(session_id, decision_state).values():
            schemas.extend(tool.schema() for tool in loaded.tools)
        return schemas

    def catalog_instruction(
        self,
        session_id: str,
        decision_state: DecisionState | None = None,
    ) -> str:
        descriptors = self._catalogs.get(session_id, ())
        if not descriptors:
            return "当前没有可加载的外部 Toolset。"
        lines = [
            "可按需加载以下 Toolset。需要其中能力时，调用 load_toolset；"
            "加载后的工具从下一次决策开始可用。"
            "只能调用本次决策可用工具中的精确名称：",
        ]
        for descriptor in descriptors:
            lines.append(f"- {descriptor.id}: {descriptor.description}")
        return "\n".join(lines)

    async def load(
        self,
        session_id: str,
        toolset_id: str,
        *,
        activation_command_id: str | None = None,
    ) -> dict[str, object]:
        if type(toolset_id) is not str or not toolset_id:
            return {
                "ok": False,
                "code": "INVALID_ARGUMENT",
                "data": {"toolset_id": toolset_id},
                "error": "toolset_id 必须是非空字符串",
            }
        available = {item.id: item for item in self._catalogs.get(session_id, ())}
        if toolset_id not in available:
            return {
                "ok": False,
                "code": "TOOLSET_NOT_FOUND",
                "data": {"toolset_id": toolset_id},
                "error": f"能力目录中没有 Toolset {toolset_id}",
                "hint": "请从可选 Toolset 目录中选择有效 ID。",
            }
        current = available[toolset_id]
        existing = self._loaded.get(session_id, {}).get(toolset_id)
        if existing is not None and existing.revision == current.revision:
            return _loaded_payload(toolset_id, current.revision, existing.tools)
        provider = self._provider_for(toolset_id)
        try:
            tools = await provider.load(toolset_id, current.revision)
        except ToolsetLoadError as exc:
            return {
                "ok": False,
                "code": exc.code,
                "data": {"toolset_id": toolset_id, **exc.data},
                "error": exc.message,
                "hint": exc.hint,
            }
        conflict = self._conflicting_names(toolset_id, tools)
        if conflict:
            return {
                "ok": False,
                "code": "TOOL_NAME_CONFLICT",
                "data": {"toolset_id": toolset_id, "names": conflict},
                "error": "Toolset 工具名与已有工具冲突",
                "hint": "请更换 Server ID 或工具名后重试。",
            }
        self._bind_loaded(
            session_id,
            toolset_id,
            current.revision,
            tools,
            activation_command_id,
        )
        return _loaded_payload(toolset_id, current.revision, tools)

    async def rehydrate(
        self,
        session_id: str,
        events: Sequence[Event],
    ) -> tuple[ToolsetActivation, ...]:
        """用 Journal 投影恢复可丢弃缓存，不向 Runtime 写入领域状态。"""

        activations = project_toolset_activations(events)
        restored: dict[str, _LoadedSet] = {}
        for activation in activations:
            tools = self._provider_for(activation.toolset_id).restore(
                activation.toolset_id,
                activation.revision,
                activation.tools,
            )
            conflict = self._conflicting_names(activation.toolset_id, tools)
            if conflict:
                raise ToolsetLoadError(
                    "TOOL_NAME_CONFLICT",
                    "Toolset 工具名与已有工具冲突",
                    data={
                        "toolset_id": activation.toolset_id,
                        "names": conflict,
                    },
                )
            self._bind_tools(activation.toolset_id, tools)
            restored[activation.toolset_id] = _LoadedSet(
                activation.revision,
                tools,
                activation.command_id,
            )
        if restored:
            self._loaded[session_id] = restored
        else:
            self._loaded.pop(session_id, None)
        return activations

    def _conflicting_names(
        self,
        toolset_id: str,
        tools: tuple[LoadedTool, ...],
    ) -> list[str]:
        names = [tool.name for tool in tools]
        duplicates = {
            name for name in names if names.count(name) > 1
        }
        conflicts = {
            name
            for name in names
            if name in self._reserved
            or (
                name in self._tool_owners
                and self._tool_owners[name] != toolset_id
            )
        }
        return sorted(duplicates | conflicts)

    def _bind_tools(
        self,
        toolset_id: str,
        tools: tuple[LoadedTool, ...],
    ) -> None:
        runtime = self._runtime
        if runtime is None:
            raise RuntimeError("ToolSurface 尚未绑定 Runtime")
        for tool in tools:
            runtime.bind_tool(
                tool.name,
                ToolBinding(
                    _loaded_handler(tool, self._gateway, self._settings),
                    requires_authorization=tool.requires_authorization,
                ),
            )
            self._tool_owners[tool.name] = toolset_id

    def _bind_loaded(
        self,
        session_id: str,
        toolset_id: str,
        revision: int,
        tools: tuple[LoadedTool, ...],
        activation_command_id: str | None,
    ) -> None:
        self._bind_tools(toolset_id, tools)
        self._loaded.setdefault(session_id, {})[toolset_id] = _LoadedSet(
            revision,
            tools,
            activation_command_id,
        )

    def _provider_for(self, toolset_id: str) -> ToolsetProvider:
        for provider in self._providers:
            if provider.handles(toolset_id):
                return provider
        raise ToolsetLoadError(
            "TOOLSET_NOT_FOUND",
            f"能力目录中没有 Toolset {toolset_id}",
            data={"toolset_id": toolset_id},
        )

    def _visible_loaded(
        self,
        session_id: str,
        decision_state: DecisionState | None,
    ) -> dict[str, _LoadedSet]:
        loaded = self._loaded.get(session_id, {})
        if decision_state is None:
            return loaded
        visible: dict[str, _LoadedSet] = {}
        for toolset_id, item in loaded.items():
            if item.activation_command_id is None:
                visible[toolset_id] = item
                continue
            command = decision_state.command(item.activation_command_id)
            if (
                command.outcome is not None
                and command.outcome.status is OutcomeStatus.SUCCEEDED
            ):
                visible[toolset_id] = item
        return visible


def load_toolset_binding(surface: ToolSurface) -> dict[str, ToolBinding]:
    async def handler(
        context: AttemptContext,
        arguments: Mapping[str, object],
    ) -> object:
        return await surface.load(
            context.session_id,
            arguments.get("toolset_id"),
            activation_command_id=context.command_id,
        )

    return {LOAD_TOOLSET: ToolBinding(handler)}


def _loaded_payload(
    toolset_id: str,
    revision: int,
    tools: tuple[LoadedTool, ...],
) -> dict[str, object]:
    return {
        "ok": True,
        "code": "TOOLSET_LOADED",
        "data": {
            "toolset_id": toolset_id,
            "revision": revision,
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                    "requires_authorization": tool.requires_authorization,
                    "provider_data": dict(tool.provider_data),
                }
                for tool in tools
            ],
        },
    }


def _loaded_handler(
    tool: LoadedTool,
    gateway: ArtifactGateway | None,
    settings: ModelContextSettings,
):
    async def handler(
        context: AttemptContext,
        arguments: Mapping[str, object],
    ) -> object:
        result = await tool.execute(arguments)
        if gateway is None:
            return result
        return externalize_tool_result(
            result,
            context.session_id,
            gateway,
            settings,
        )

    return handler
