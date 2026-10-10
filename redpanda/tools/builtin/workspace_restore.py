from collections.abc import Awaitable, Callable

from pydantic import BaseModel, Field
from typing import Literal

from redpanda.tools.spec import PydanticParameters, ToolSpec


RESTORE_WORKSPACE = "restore_workspace"
RESTORE_DESCRIPTION = (
    "撤回指定 tool_call_id 所在整批及其后助手操作造成的工作区文件变化。"
    "引用当前上下文中的调用 id；同批任意调用指向同一个回退点。"
    "回退可以撤销：再次调用本工具并引用这次 restore_workspace 的调用 id。"
    "policy=preserve 保留用户后来改变的值；original 允许恢复助手改变位置的原值。"
    "两种策略都保留助手未改变的位置；.gitignore 不影响回退范围。"
    "范围包含同一工作区其间其他会话的助手修改。"
    "成功结果说明实际改动的文件或目录，以及保留后续值的位置；同一路径可能同时出现。"
    "unchanged 表示本次没有改变文件，preserved 表示因保留后续值而未改变文件，不能据此宣称已完全撤销。"
    "仅处理受支持的工作区文件变化，不撤销命令通过绝对路径造成的修改、安装、网络操作或聊天历史。必须单独调用。"
)
ISOLATED_RESTORE_DESCRIPTION = (
    "把你的工作区恢复到指定 tool_call_id 所在整批工具调用之前的文件状态。"
    "引用当前上下文中的调用 id；同批任意调用指向同一个回退点。"
    "回退可以撤销：再次调用本工具并引用这次 restore_workspace 的调用 id。"
    "policy=preserve 保留后来改变的值，original 恢复助手改变位置的原值；.gitignore 不影响回退范围。"
    "成功结果说明实际改动的文件和保留后续值的文件；没有实际改动不能宣称已完全撤销。"
    "不影响用户的文件，不撤销聊天历史。必须单独调用。"
)


class RestoreWorkspaceInput(BaseModel):
    tool_call_id: str = Field(min_length=1, description="要撤销的工具调用 id；恢复到其所在那一批调用之前")
    policy: Literal["original", "preserve"] = Field(default="preserve", description="preserve 保留用户后续值；original 允许覆盖助手改变位置的后续值")


def create_workspace_restore_spec(
    restore: Callable[[str, str], Awaitable[dict]],
    *,
    isolated: bool = False,
) -> ToolSpec:
    async def handler(raw: RestoreWorkspaceInput):
        return await restore(raw.tool_call_id, raw.policy)

    return ToolSpec(
        name=RESTORE_WORKSPACE,
        description=ISOLATED_RESTORE_DESCRIPTION if isolated else RESTORE_DESCRIPTION,
        parameters=PydanticParameters(RestoreWorkspaceInput),
        handler=handler,
        exclusive_batch=True,
    )
