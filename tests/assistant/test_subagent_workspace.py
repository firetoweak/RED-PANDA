import asyncio

from konden.assistant.builtin_tools import build_builtin_tools
from tests.fixtures.workspaces import workspace_record


def test_shared_task_uses_the_same_files_and_write_authorization(tmp_path):
    async def scenario():
        (tmp_path / "uncommitted.txt").write_text("parent work")
        parent = await build_builtin_tools(workspace_record(tmp_path))
        child = await build_builtin_tools(workspace_record(tmp_path))
        args = {"path": "child.txt", "content": "child work"}
        assert child.requires_authorization("write_file")(args) is True
        assert parent.requires_authorization("write_file")(args) is True
        assert child.requires_authorization("write_file") is not False
        result = await child.execute("write_file", {"path": "child.txt", "content": "child work"})
        assert result["ok"] is True
        assert (tmp_path / "child.txt").read_text() == "child work"
        result = await child.execute("read_file", {"path": "uncommitted.txt"})
        assert "parent work" in str(result)
    asyncio.run(scenario())
