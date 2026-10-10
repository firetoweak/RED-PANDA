import asyncio
import os

import pytest

from redpanda.assistant.builtin_tools import build_builtin_tools, subagent_review_tools
from redpanda.sandbox.files.children import child_root, child_workspace
from redpanda.paths import RedPandaHome
from redpanda.sandbox.registry import workspace_view
from tests.fixtures.workspaces import workspace_record


def test_child_does_not_inherit_full_access_or_write_outside_its_tree(tmp_path):
    async def scenario():
        parent_root = tmp_path / "parent"
        child_root = tmp_path / "child"
        parent_root.mkdir()
        child_root.mkdir()
        parent = workspace_record(parent_root, full_access=True)
        child = child_workspace(parent, child_root)
        assert child.workspace_id == parent.workspace_id
        assert child.full_access is False
        assert [root.path for root in workspace_view(child).roots] == [child_root]
        runner = await build_builtin_tools(child, isolated=True)
        assert runner.requires_authorization("write_file") is False
        result = await runner.execute("write_file", {"path": str(parent_root / "escape"), "content": "outside"})
        assert result["ok"] is False
        assert not (parent_root / "escape").exists()
        result = await runner.execute("write_file", {"path": "inside", "content": "inside"})
        assert result["ok"] is True
        assert (child_root / "inside").read_text() == "inside"
    asyncio.run(scenario())


def test_only_merge_requires_authorization_and_is_exclusive():
    async def operation(*args, **kwargs):
        return {"ok": True, "code": "OK"}
    schemas, bindings, exclusive = subagent_review_tools(operation)
    assert {schema["function"]["name"] for schema in schemas} == set(bindings)
    assert bindings["compare_subagent"].requires_authorization is False
    assert bindings["merge_subagent"].requires_authorization is True
    assert exclusive == {"merge_subagent"}


@pytest.mark.skipif(os.name != "nt", reason="Windows 文件路径长度契约")
def test_generated_child_can_write_with_long_home(tmp_path):
    async def scenario():
        home = RedPandaHome(tmp_path / "home")
        root = child_root(home, "parent", "parent/sub-command_" + "a" * 32)
        root.mkdir(parents=True)
        runner = await build_builtin_tools(child_workspace(workspace_record(tmp_path), root), isolated=True)
        result = await runner.execute("write_file", {"path": "result.txt", "content": "marker\n"})
        assert result["ok"] is True
        assert (root / "result.txt").read_text() == "marker\n"
    asyncio.run(scenario())
