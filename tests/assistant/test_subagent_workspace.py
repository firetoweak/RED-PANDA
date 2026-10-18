import asyncio

from redpanda.assistant.builtin_tools import build_builtin_tools, subagent_review_tools
from tests.fixtures.workspaces import workspace_record


def test_delegated_task_writes_its_tree_without_approval_but_not_outside(tmp_path):
    async def scenario():
        parent_root = tmp_path / "parent"
        child_root = tmp_path / "child"
        parent_root.mkdir()
        child_root.mkdir()
        runner = await build_builtin_tools(workspace_record(child_root), isolated=True)
        assert runner.requires_authorization("write_file") is False
        result = await runner.execute("write_file", {"path": str(parent_root / "escape"), "content": "outside"})
        assert result["ok"] is False
        assert not (parent_root / "escape").exists()
        result = await runner.execute("write_file", {"path": "inside", "content": "inside"})
        assert result["ok"] is True
        assert (child_root / "inside").read_text() == "inside"
    asyncio.run(scenario())


def test_agent_branch_merge_needs_no_authorization_and_stays_exclusive():
    async def operation(*args, **kwargs):
        return {"ok": True, "code": "OK"}
    schemas, bindings, exclusive = subagent_review_tools(operation)
    merge = next(schema["function"] for schema in schemas if schema["function"]["name"] == "merge_subagent")
    assert {schema["function"]["name"] for schema in schemas} == set(bindings)
    assert bindings["compare_subagent"].requires_authorization is False
    assert bindings["merge_subagent"].requires_authorization is False
    assert "你的分支" in merge["description"] and "授权" in merge["description"]
    assert exclusive == {"merge_subagent"}
