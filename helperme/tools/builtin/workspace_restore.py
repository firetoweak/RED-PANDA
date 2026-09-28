from collections.abc import Awaitable, Callable

from pydantic import BaseModel, Field

from helperme.tools.spec import PydanticParameters, ToolSpec


RESTORE_WORKSPACE = "restore_workspace"
RESTORE_DESCRIPTION = (
    "恢复到指定 tool_call_id 所在整批工具调用之前的工作区文件状态。"
    "引用当前上下文中的调用 id；同批任意调用指向同一个回退点。"
    "回退可以撤销：再次调用本工具并引用这次 restore_workspace 的调用 id。"
    ".gitignore 排除的内容（依赖目录、构建产物、本机配置）不会被恢复，保持现状；"
    "不撤销命令等外部副作用，也不撤销聊天历史；"
    "工作区里不是你改的文件也会一起回到那时的状态。必须单独调用。"
)
ISOLATED_RESTORE_DESCRIPTION = (
    "把你的工作区恢复到指定 tool_call_id 所在整批工具调用之前的文件状态。"
    "引用当前上下文中的调用 id；同批任意调用指向同一个回退点。"
    "回退可以撤销：再次调用本工具并引用这次 restore_workspace 的调用 id。"
    ".gitignore 排除的内容不会被恢复，保持现状；"
    "不影响用户的文件，不撤销聊天历史。必须单独调用。"
)


class RestoreWorkspaceInput(BaseModel):
    tool_call_id: str = Field(min_length=1, description="要撤销的工具调用 id；恢复到其所在那一批调用之前")


def create_workspace_restore_spec(
    restore: Callable[[str], Awaitable[dict]],
    *,
    isolated: bool = False,
) -> ToolSpec:
    async def handler(raw: RestoreWorkspaceInput):
        return await restore(raw.tool_call_id)

    return ToolSpec(
        name=RESTORE_WORKSPACE,
        description=ISOLATED_RESTORE_DESCRIPTION if isolated else RESTORE_DESCRIPTION,
        parameters=PydanticParameters(RestoreWorkspaceInput),
        handler=handler,
        exclusive_batch=True,
    )
