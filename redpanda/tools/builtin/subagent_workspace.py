from pathlib import PurePosixPath, PureWindowsPath

from pydantic import BaseModel, Field, field_validator

from redpanda.tools.spec import PydanticParameters, ToolSpec


class SubagentChangesInput(BaseModel):
    tool_call_id: str = Field(min_length=1, description="当前会话已提交的 delegate 调用 id")
    paths: list[str] = Field(default_factory=list, description="为空只列文件；指定相对路径时返回这些文件的 diff")
    offset: int = Field(default=0, ge=0, description="diff 的字符续读位置；首次为 0，续查使用 next_offset，并保持 paths 不变")

    @field_validator("paths")
    @classmethod
    def relative_paths(cls, paths):
        for path in paths:
            if not path or PureWindowsPath(path).anchor or ".." in PurePosixPath(path.replace("\\", "/")).parts:
                raise ValueError("paths 必须是工作树内的相对路径")
        return [path.replace("\\", "/") for path in paths]


class MergeSubagentInput(BaseModel):
    tool_call_id: str = Field(min_length=1, description="要验收合入的 delegate 调用 id")


def create_subagent_workspace_specs(review):
    async def compare(raw: SubagentChangesInput):
        return await review(raw.tool_call_id, raw.paths, offset=raw.offset)

    async def merge(raw: MergeSubagentInput):
        return await review(raw.tool_call_id, merge=True)

    return (
        ToolSpec("compare_subagent", "查看已交回子会话相对委派时文件状态的变化；先列文件，再指定 paths 看 diff。"
                 "next_offset 非空时保持 paths 不变并用 offset 续读；截断片段不能作为完整 diff。二进制文件只列变化，不展示正文。",
                 PydanticParameters(SubagentChangesInput), compare),
        ToolSpec("merge_subagent", "把已交回子会话的修改合进用户的文件，与你这边的改动合并，需要授权。"
                 "成功结果列出实际改动文件；changed_count=0 表示本次没有改变文件。合入后用 get_changes 核对当前结果。"
                 "冲突时不改动任何文件，只返回冲突清单。必须单独调用。",
                 PydanticParameters(MergeSubagentInput), merge,
                 exclusive_batch=True, requires_authorization=True),
    )
