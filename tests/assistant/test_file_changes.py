import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from redpanda.assistant.conversations import AssistantQueries
from redpanda.assistant.host.session_store import SessionStore
from redpanda.paths import RedPandaHome
from redpanda.runtime import AgentRuntime, InvokeTool, ModelDecision, SqliteJournal, ToolBinding
from redpanda.sandbox.files import operation_id
from redpanda.sandbox.registry import WorkspaceRegistry
from tests.assistant.test_runner import ScriptedDecisionMaker


def test_branch_review_uses_original_operation_owner_without_starting_worker(tmp_path):
    async def run():
        root = tmp_path / "project"
        root.mkdir()
        registry = WorkspaceRegistry.load(tmp_path / "workspaces.json")
        workspace = registry.register_path(root)
        store = SessionStore(tmp_path / "sessions")
        await store.create("source", workspace_id=workspace.workspace_id)
        runtime = AgentRuntime(SqliteJournal(store.require("source")), ScriptedDecisionMaker((
            lambda _: ModelDecision(command_requests=(InvokeTool("edit", ()),)),
        )), {"edit": ToolBinding(AsyncMock(), requires_authorization=True)})
        await runtime.receive_user_message("source", "edit", delivery_id="input")
        step = (await runtime.advance("source")).step
        # The query also needs to find an operation whose outcome has not yet
        # reached the Journal; publication is the file evidence boundary.
        source_events = await runtime.snapshot("source")
        from redpanda.runtime.journal.sqlite import HistorySpan
        store.path("branch").parent.mkdir()
        branch = SqliteJournal(store.path("branch"))
        await branch.create_branch("branch", (HistorySpan(
            store.require("source").relative_to(store.root).as_posix(), "source", 1, source_events[-1].sequence,
        ),))
        host = SimpleNamespace(home=RedPandaHome(tmp_path / "home"), workspaces=registry)
        files = SimpleNamespace(review=AsyncMock(return_value={"files": [], "added": 0, "removed": 0, "partial": False}))
        with patch("redpanda.assistant.conversations.workspace_files", return_value=files):
            result = await AssistantQueries(store, host).file_changes("branch")
        assert result["files"] == []
        files.review.assert_awaited_once_with(
            tuple(operation_id("source", command.command_id) for command in step.commands), path=None, offset=0)

    asyncio.run(run())
