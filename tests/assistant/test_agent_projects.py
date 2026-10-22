"""Compensating a failed mount must respect the recipient's durable acceptance."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from redpanda.assistant.agents import AgentCreated, ProjectLinked, fact_arguments
from redpanda.assistant.host.agent_projects import AgentProjects
from redpanda.assistant.host.agent_roster import AgentRoster
from redpanda.assistant.host.session_store import SessionStore
from redpanda.runtime import AgentRuntime, SqliteJournal


class ProjectLinkAcknowledgementTest(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_depends_on_acceptance_and_preserves_the_original_error(self):
        for accepted in (False, True):
            with self.subTest(accepted=accepted), TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                directory = root / "bot"
                directory.mkdir()
                store = SessionStore(root / "sessions")
                await store.create("bot", directory=directory, initial_fact=fact_arguments(
                    AgentCreated(name="Ada", directory=str(directory)), delivery_id="created"))
                journal = SqliteJournal(store.require("bot"))
                worktree = directory / "project"
                worktree.mkdir()
                linked = ProjectLinked(workspace_id="project", name="project", worktree=str(worktree), branch="bot")
                lost = EOFError("acknowledgement lost")

                async def receive(_operation, session_id, arguments):
                    if accepted:
                        await AgentRuntime(journal, None, {}).receive_domain_fact(session_id, **arguments)
                    raise lost

                remove = Mock(side_effect=lambda _project, tree: tree.path.rmdir())
                host = SimpleNamespace(store=store, agents=AgentRoster(store), request=AsyncMock(side_effect=receive),
                                       agent_worktrees=SimpleNamespace(remove=remove))
                projects = AgentProjects(host)
                with patch.object(projects, "prepare", return_value=linked):
                    with self.assertRaises(EOFError) as raised:
                        await projects.link("bot", SimpleNamespace(workspace_id="project"), "link:original")
                self.assertIs(raised.exception, lost)
                self.assertEqual(worktree.exists(), accepted)
                self.assertEqual(remove.call_count, int(not accepted))
                restored = await AgentRoster(store).by_session("bot")
                cached = await host.agents.by_session("bot")
                expected = (linked,) if accepted else ()
                self.assertEqual(restored.projects, expected)
                self.assertEqual(cached.projects, expected)
