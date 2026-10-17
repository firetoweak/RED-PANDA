from collections.abc import Awaitable, Callable

from pydantic import BaseModel, Field
from typing import Literal

from redpanda.tools.spec import PydanticParameters, ToolSpec


RESTORE_WORKSPACE = "restore_workspace"
RESTORE_DESCRIPTION = (
    "撤回 tool_call_id 所在整批及之后的助手文件修改，包括同一工作区其他会话在此期间的助手修改；"
    "同批任意调用 id 指向同一回退点，.gitignore 不影响范围。不撤销工作区外的副作用和聊天记录。"
    "回退本身也能用它的调用 id 再撤回。"
    "结果列出实际改动和保留了后续值的位置；unchanged 或 preserved 表示没有完全撤回。"
    "遇到混合修改、保留或失败时，读取当前内容后按用户目标用文件工具处理，不要换 policy 反复重试。"
    "必须单独调用。"
)
ISOLATED_RESTORE_DESCRIPTION = (
    "把你的工作区恢复到 tool_call_id 所在整批调用之前的文件状态；"
    "同批任意调用 id 指向同一回退点，.gitignore 不影响范围。回退本身也能用它的调用 id 再撤回。"
    "结果列出实际改动和保留了后续值的文件；没有实际改动时不能宣称已撤回。"
    "失败时核对当前内容，必要时用文件工具处理，不要反复重试。不撤销聊天记录。必须单独调用。"
)


class RestoreWorkspaceInput(BaseModel):
    tool_call_id: str = Field(min_length=1, description="要撤销的工具调用 id；恢复到其所在那一批调用之前")
    policy: Literal["original", "preserve"] = Field(default="preserve", description="preserve（默认）：位置后来被改过就保留后来的值；original：这些位置也恢复为原值。两种都不动助手没改过的位置")


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
