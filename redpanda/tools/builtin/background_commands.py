from pydantic import BaseModel, Field

from redpanda.execution.api import CommandControl
from redpanda.tools.spec import EmptyInput, PydanticParameters, ToolSpec


class CommandInput(BaseModel):
    command_id: str = Field(min_length=1, description="execute_command 或 list_commands 返回的后台命令标识")


class ReadCommandInput(CommandInput):
    stdout_offset: int = Field(default=0, ge=0, description="续读使用上次 stdout.next_offset")
    stderr_offset: int = Field(default=0, ge=0, description="续读使用上次 stderr.next_offset")


def _result(data, code):
    if data is None:
        return {"ok": False, "code": "COMMAND_NOT_FOUND", "error": "本会话没有该命令，请查询 list_commands。"}
    return {"ok": True, "code": code, "data": data}


def create_command_specs(commands: CommandControl) -> list[ToolSpec]:
    async def read(raw: ReadCommandInput):
        return _result(await commands.read(raw.command_id, raw.stdout_offset, raw.stderr_offset), "COMMAND_READ")

    async def stop(raw: CommandInput):
        return _result(await commands.stop(raw.command_id), "COMMAND_STOPPED")

    async def list_commands(_raw: EmptyInput):
        return _result(await commands.list(), "COMMANDS_LISTED")

    return [
        ToolSpec(name="read_command", description=(
            "读取本会话命令的当前状态和有限长度的输出，不等待退出。续读用 next_offset，has_more 表示还有输出。"
            "后台命令结束会主动通知，不要循环查询。running 不表示服务已就绪。"
        ), parameters=PydanticParameters(ReadCommandInput), handler=read, requires_authorization=False),
        ToolSpec(name="stop_command", description="终止指定命令及其子进程；已产生的副作用不会撤销。后台命令终止后也会通知。",
                 parameters=PydanticParameters(CommandInput), handler=stop, requires_authorization=False),
        ToolSpec(name="list_commands", description="列出本会话后台命令的当前状态，包括已结束的记录。",
                 parameters=PydanticParameters(EmptyInput), handler=list_commands, requires_authorization=False),
    ]
