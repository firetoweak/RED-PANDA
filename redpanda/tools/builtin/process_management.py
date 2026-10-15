from __future__ import annotations

from pydantic import BaseModel, Field

from redpanda.execution.command import CommandStartError, ShellNotFoundError
from redpanda.execution.api import ProcessControl
from redpanda.filesystem import FileAccess
from redpanda.filesystem.workspace import EnvironmentInputError
from redpanda.tools.results import environment_error
from redpanda.tools.spec import EmptyInput, PydanticParameters, ToolSpec


class StartProcessInput(BaseModel):
    command: str = Field(min_length=1, pattern=r"^[^\x00]+$", description="要在本机 Shell 中执行的非交互命令")
    cwd: str | None = Field(default=None, description="启动目录；省略或相对路径均从工作区开始")


class ProcessInput(BaseModel):
    process_id: str = Field(min_length=1, description="本会话 start_process 或 list_processes 返回的进程标识")


class ReadProcessInput(ProcessInput):
    stdout_offset: int = Field(default=0, ge=0, description="读取起点；续读使用上次 stdout.next_offset")
    stderr_offset: int = Field(default=0, ge=0, description="读取起点；续读使用上次 stderr.next_offset")


def _result(data, code):
    if data is None:
        return {"ok": False, "code": "PROCESS_NOT_FOUND", "error": "本会话没有该进程记录；请查询 list_processes。"}
    return {"ok": True, "code": code, "data": data}


def create_process_specs(binding: FileAccess, processes: ProcessControl) -> list[ToolSpec]:
    async def start(raw: StartProcessInput):
        if not raw.command.strip():
            return {"ok": False, "code": "EMPTY_COMMAND", "error": "command 不能为空"}
        try:
            cwd = binding.resolver.resolve("." if raw.cwd is None else raw.cwd, access="write", project=False).native_path
        except EnvironmentInputError as exc:
            return environment_error(exc)
        if not cwd.is_dir():
            return {"ok": False, "code": "CWD_NOT_A_DIRECTORY", "error": "启动目录不存在或不是目录"}
        try:
            data = await processes.start(raw.command, cwd)
        except ShellNotFoundError as exc:
            return {"ok": False, "code": "SHELL_NOT_FOUND", "error": str(exc)}
        except CommandStartError as exc:
            return {"ok": False, "code": "COMMAND_START_FAILED", "error": str(exc)}
        return _result(data, "PROCESS_STARTED")

    async def read(raw: ReadProcessInput):
        return _result(await processes.read(raw.process_id, raw.stdout_offset, raw.stderr_offset), "PROCESS_READ")

    async def stop(raw: ProcessInput):
        return _result(await processes.stop(raw.process_id), "PROCESS_STOPPED")

    async def list_processes(_raw: EmptyInput):
        return _result(await processes.list(), "PROCESSES_LISTED")

    return [
        ToolSpec(
            name="start_process",
            description=(
                "在真实本机环境启动需要跨工具调用持续运行的非交互程序，例如开发服务器。"
                "文件修改不提供回退保证；启动目录须在本会话允许范围内，启动前需授权。"
                "返回进程标识后可继续其他操作；启动成功不代表服务已就绪，请读取输出并检查服务。"
                "切换会话或暂停当前任务不会停止程序；明确停止、归档会话或关闭应用时终止其进程树。"
                "程序意外退出不会自动重启；应用重新启动后不保留进程记录。"
                "不支持交互输入、UAC、密码或硬件确认，不提供系统和网络隔离。"
                "各次启动独立，不继承先前命令的环境激活。仅 Windows 提供此能力。"
            ),
            parameters=PydanticParameters(StartProcessInput), handler=start, requires_authorization=True,
        ),
        ToolSpec(name="read_process", description=(
            "查询本会话持续进程的状态并读取有限长度的 stdout/stderr；不会等待进程退出。"
            "续读使用各流的 next_offset；has_more 表示当前还有未读输出。"
            "running 表示进程树尚未结束，不代表服务健康或已就绪。"
        ), parameters=PydanticParameters(ReadProcessInput), handler=read, requires_authorization=False),
        ToolSpec(name="stop_process", description="终止本会话指定持续进程及其子进程并确认退出；已产生的副作用不会撤销。",
                 parameters=PydanticParameters(ProcessInput), handler=stop, requires_authorization=False),
        ToolSpec(name="list_processes", description="列出本会话启动的持续进程及其当前状态，包括已结束的记录；不列出本机其他进程。",
                 parameters=PydanticParameters(EmptyInput), handler=list_processes, requires_authorization=False),
    ]
