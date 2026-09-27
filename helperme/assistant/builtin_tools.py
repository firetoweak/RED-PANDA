from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from helperme.assistant.tool_results import runtime_tool_result
from helperme.runtime.model import AuthorizationPolicy
from helperme.sandbox.api import EnvironmentSelection
from helperme.sandbox.local.provider import create_local_environment_provider
from helperme.sandbox.registry import WorkspaceRecord, workspace_view
from helperme.sandbox.workspace import (
    FilesystemPermission,
    PermissionBinding,
    RootBinding,
    WorkspaceScope,
    WorkspaceViewSnapshot,
)
from helperme.tools.executor import ToolsExecutor
from helperme.tools.registry import BUILTIN_TOOL_REGISTRY, ToolRegistry
from helperme.tools.builtin import (
    CommandInterrupts,
    create_environment_tool_specs,
    create_workspace_restore_spec,
)


@dataclass(frozen=True, slots=True)
class BuiltinToolRunner:
    schemas: tuple[dict[str, object], ...]
    command_interrupts: CommandInterrupts
    _executor: ToolsExecutor

    async def execute(self, name: str, arguments: Mapping[str, object]) -> object:
        return runtime_tool_result(
            await self._executor.execute_parsed(name, arguments)
        )

    def names(self) -> tuple[str, ...]:
        names: list[str] = []
        for schema in self.schemas:
            if set(schema) != {"type", "function"} or schema["type"] != "function":
                raise ValueError("builtin tool schema envelope 无效")
            function = schema["function"]
            if not isinstance(function, dict):
                raise ValueError("builtin tool schema function 必须是 object")
            name = function.get("name")
            if type(name) is not str or not name:
                raise ValueError("builtin tool schema name 必须是非空 string")
            names.append(name)
        if len(names) != len(set(names)):
            raise ValueError("builtin tool schemas 包含重复 name")
        return tuple(names)

    def requires_authorization(self, name: str) -> bool | AuthorizationPolicy:
        spec = self._executor.registry.get(name)
        if spec is None:
            raise KeyError(name)
        return spec.requires_authorization


async def build_builtin_tools(
    workspace: WorkspaceRecord,
    *,
    materials_root: Path | None = None,
) -> BuiltinToolRunner:
    view = workspace_view(workspace)
    if materials_root is not None:
        materials_root.mkdir(parents=True, exist_ok=True)
        view = WorkspaceViewSnapshot((
            *view.roots,
            RootBinding("session_materials", WorkspaceScope.MATERIALS, materials_root),
        ))
    provider = create_local_environment_provider()
    binding = await provider.attach(EnvironmentSelection(
        environment_id=provider.environment_id,
        workspace_view=view,
        cwd=str(workspace.task_root),
    ))
    if materials_root is not None:
        binding = replace(
            binding,
            permission_binding=PermissionBinding(
                tuple(
                    (root_id, FilesystemPermission.READ_ONLY if root_id == "session_materials" else access)
                    for root_id, access in binding.permission_binding.filesystem
                ),
                network_access=binding.permission_binding.network_access,
            ),
        )
    interrupts = CommandInterrupts()
    registry = BUILTIN_TOOL_REGISTRY.clone()
    for spec in create_environment_tool_specs(binding, interrupts):
        registry.register(spec)
    return BuiltinToolRunner(
        schemas=tuple(registry.get_tools()),
        command_interrupts=interrupts,
        _executor=ToolsExecutor(registry),
    )


def workspace_restore_tool(operation: Callable[[str, str], Awaitable[dict]]):
    """声明来自 ToolSpec；每次执行绑定 Runtime 提供的调用身份。"""
    from helperme.runtime import ToolBinding

    def executor(command_id):
        async def restore(target):
            return await operation(command_id, target)
        spec = create_workspace_restore_spec(restore)
        registry = ToolRegistry()
        registry.register(spec)
        return spec, ToolsExecutor(registry)

    # 这里只取声明；执行器在收到实际 AttemptContext 时构造。
    spec, _ = executor(None)

    async def handler(context, arguments):
        bound, tools = executor(context.command_id)
        return runtime_tool_result(await tools.execute_parsed(bound.name, arguments))

    binding = ToolBinding(handler, requires_authorization=spec.requires_authorization)
    exclusive = frozenset({spec.name}) if spec.exclusive_batch else frozenset()
    return spec.to_openai_tool(), binding, exclusive
