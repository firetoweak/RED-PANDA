from __future__ import annotations

import os
from collections.abc import Mapping

from redpanda.execution.api import CommandEnvironment


def create_host_command_runner(environment: Mapping[str, str]):
    if os.name == "nt":
        from .powershell import CommandEnvironmentPolicy, PowerShellCommandRunner
        return PowerShellCommandRunner(
            environment=environment,
            environment_policy=CommandEnvironmentPolicy(
                forward_names=tuple(environment), refresh_path=False,
            ),
        )
    from .bash import BashCommandEnvironmentPolicy, BashCommandRunner
    return BashCommandRunner(
        environment=environment,
        environment_policy=BashCommandEnvironmentPolicy(forward_names=tuple(environment)),
    )


def create_local_command_environment(
    command_environment: Mapping[str, str] | None = None,
) -> CommandEnvironment:
    # 独立命令来源，包含用户准备的项目变量；不读取随后改变的产品环境。
    environment = dict(os.environ if command_environment is None else command_environment)
    runner = create_host_command_runner(environment)
    return CommandEnvironment(
        runner,
        shell_name="powershell" if os.name == "nt" else "bash",
        shell_path=runner.executable,
    )
