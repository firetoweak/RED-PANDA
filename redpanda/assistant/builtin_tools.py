from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from redpanda.assistant.tool_results import runtime_tool_result
from redpanda.execution.api import ProcessControl
from redpanda.execution.local.provider import create_local_command_environment
from redpanda.filesystem import FileAccess
from redpanda.filesystem.registry import WorkspaceRecord, workspace_view
from redpanda.filesystem.workspace import (
    FilesystemPermission,
    PermissionBinding,
    RootBinding,
    WorkspaceScope,
    WorkspaceViewSnapshot,
)
from redpanda.runtime.model import AuthorizationPolicy, rejected_tool_arguments
from redpanda.tools.builtin import (
    CommandInterrupts,
    create_process_specs,
    create_subagent_workspace_specs,
    create_workspace_tool_specs,
)
from redpanda.tools.executor import ToolsExecutor
from redpanda.tools.registry import BUILTIN_TOOL_REGISTRY, ToolRegistry


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
        policy = spec.requires_authorization
        if not callable(policy):
            return policy

        def verdict(arguments):
            result = policy(arguments)
            if type(result) is bool:
                return result
            return rejected_tool_arguments(
                json.dumps(dict(arguments), ensure_ascii=False),
                code=result["code"], error=result["error"], hint=result["hint"],
            )
        return verdict


async def build_builtin_tools(
    workspace: WorkspaceRecord,
    *,
    materials_root: Path | None = None,
    colleague_root: Path | None = None,
    isolated: bool = False,
    command_environment: Mapping[str, str] | None = None,
    processes: ProcessControl | None = None,
) -> BuiltinToolRunner:
    view = workspace_view(workspace)
    read_only = set()
    if materials_root is not None:
        materials_root.mkdir(parents=True, exist_ok=True)
        view = WorkspaceViewSnapshot((
            *view.roots,
            RootBinding("session_materials", WorkspaceScope.MATERIALS, materials_root),
        ))
        read_only.add("session_materials")
    if colleague_root is not None:
        # The Agent's own worktree is a deeper root inside this one and stays writable.
        view = WorkspaceViewSnapshot((
            *view.roots,
            RootBinding("colleague_worktrees", WorkspaceScope.COLLEAGUES, colleague_root),
        ))
        read_only.add("colleague_worktrees")
    commands = create_local_command_environment(command_environment)
    binding = FileAccess(
        environment_id="local",
        workspace_view=view,
        permission_binding=PermissionBinding.read_write(view),
        cwd=workspace.task_root,
    )
    if read_only:
        binding = replace(
            binding,
            permission_binding=PermissionBinding(
                tuple(
                    (root_id, FilesystemPermission.READ_ONLY if root_id in read_only else access)
                    for root_id, access in binding.permission_binding.filesystem
                ),
            ),
        )
    interrupts = CommandInterrupts()
    registry = BUILTIN_TOOL_REGISTRY.clone()
    for spec in create_workspace_tool_specs(binding, commands, interrupts):
        if isolated and spec.name in {"write_file", "apply_patch", "replace_all"}:
            spec = replace(spec, requires_authorization=False)
        registry.register(spec)
    if processes is not None:
        for spec in create_process_specs(binding, processes):
            registry.register(spec)
    return BuiltinToolRunner(
        schemas=tuple(registry.get_tools()),
        command_interrupts=interrupts,
        _executor=ToolsExecutor(registry),
    )


def subagent_review_tools(operation):
    from redpanda.runtime import ToolBinding

    specs = create_subagent_workspace_specs(operation)
    bindings = {}
    for spec in specs:
        async def handler(context, arguments, _name=spec.name):
            async def review(*args, **kwargs):
                return await operation(context.command_id, *args, **kwargs)
            registry = ToolRegistry()
            for bound in create_subagent_workspace_specs(review):
                registry.register(bound)
            executor = ToolsExecutor(registry)
            return runtime_tool_result(await executor.execute_parsed(_name, arguments))
        bindings[spec.name] = ToolBinding(handler, requires_authorization=spec.requires_authorization)
    return ([spec.to_openai_tool() for spec in specs], bindings,
            frozenset(spec.name for spec in specs if spec.exclusive_batch))
