from __future__ import annotations

import os
from pathlib import Path
from collections.abc import Mapping

from redpanda.execution.api import CommandEnvironment


def create_local_command_environment(
    command_environment: Mapping[str, str] | None = None,
    *,
    task_root: Path,
) -> CommandEnvironment:
    task_root = task_root.resolve()
    # 独立命令来源，包含用户准备的项目变量；不读取随后改变的产品环境。
    environment = dict(os.environ if command_environment is None else command_environment)
    if os.name == "nt":
        from redpanda.execution.local.powershell import (
            CommandEnvironmentPolicy, PowerShellCommandRunner,
        )

        runner = PowerShellCommandRunner(
            task_root=task_root,
            environment=environment,
            environment_policy=CommandEnvironmentPolicy(
                forward_names=tuple(environment), refresh_path=False,
            ),
        )
        shell_name = "powershell"
    else:
        from redpanda.execution.local.bash import (
            BashCommandEnvironmentPolicy, BashCommandRunner,
        )

        runner = BashCommandRunner(
            environment=environment,
            environment_policy=BashCommandEnvironmentPolicy(forward_names=tuple(environment)),
        )
        shell_name = "bash"
    return CommandEnvironment(
        runner,
        shell_name=shell_name,
        shell_path=runner.executable,
    )
