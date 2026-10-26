from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from konden.assistant.tool_results import runtime_tool_result
from konden.execution.api import CommandControl
from konden.execution.local.provider import create_local_command_environment
from konden.filesystem import FileAccess
from konden.filesystem.registry import WorkspaceRecord, workspace_view
from konden.filesystem.workspace import (
    FileWorkspace,
    FilesystemPermission,
    PermissionBinding,
    RootBinding,
    WorkspaceScope,
    WorkspaceViewSnapshot,
)
from konden.runtime.model import AuthorizationPolicy, rejected_tool_arguments
from konden.tools.builtin import (
    CommandInterrupts,
    command_result,
    current_command_id,
    create_command_specs,
    create_workspace_tool_specs,
)
from konden.tools.executor import ToolsExecutor
from konden.tools.registry import BUILTIN_TOOL_REGISTRY


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
    workspace: WorkspaceRecord | FileWorkspace,
    *,
    materials_root: Path | None = None,
    personal_root: Path | None = None,
    colleague_roots: tuple[Path, ...] = (),
    command_environment: Mapping[str, str] | None = None,
    commands: CommandControl | None = None,
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
    if personal_root is not None and personal_root != workspace.task_root:
        view = WorkspaceViewSnapshot((*view.roots, RootBinding("personal_directory", WorkspaceScope.TASK, personal_root)))
    for index, root in enumerate(colleague_roots):
        # The Bot's own directories are deeper writable roots.
        root.mkdir(parents=True, exist_ok=True)
        view = WorkspaceViewSnapshot((
            *view.roots,
            RootBinding(f"colleague_worktrees_{index}", WorkspaceScope.COLLEAGUES, root),
        ))
        read_only.add(f"colleague_worktrees_{index}")
    environment = None
    if commands is not None:
        environment = replace(create_local_command_environment(command_environment),
                              command_executor=commands, background_supported=True)
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
    for spec in create_workspace_tool_specs(binding, environment, interrupts):
        registry.register(spec)
    if commands is not None:
        for spec in create_command_specs(commands):
            registry.register(spec)
    return BuiltinToolRunner(
        schemas=tuple(registry.get_tools()),
        command_interrupts=interrupts,
        _executor=ToolsExecutor(registry),
    )



__all__ = ["build_builtin_tools", "BuiltinToolRunner", "command_result", "current_command_id"]
