from pathlib import PurePosixPath, PureWindowsPath

from pydantic import BaseModel, Field, field_validator

from redpanda.tools.spec import PydanticParameters, ToolSpec


class SubagentChangesInput(BaseModel):
    tool_call_id: str = Field(min_length=1, description="当前会话已提交的 delegate 调用 id")
    paths: list[str] = Field(default_factory=list, description="为空只列文件；指定相对路径时返回这些文件的 diff")

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
        return await review(raw.tool_call_id, raw.paths)

    async def merge(raw: MergeSubagentInput):
        return await review(raw.tool_call_id, merge=True)

    return (
        ToolSpec("compare_subagent", "查看已交回子会话相对委派时文件状态的变化；先列文件，再指定 paths 看 diff。",
                 PydanticParameters(SubagentChangesInput), compare),
        ToolSpec("merge_subagent", "把已交回子会话的修改合进用户的文件，与你这边的改动合并，需要授权。"
                 "冲突时不改动任何文件，只返回冲突清单。必须单独调用。",
                 PydanticParameters(MergeSubagentInput), merge,
                 exclusive_batch=True, requires_authorization=True),
    )
