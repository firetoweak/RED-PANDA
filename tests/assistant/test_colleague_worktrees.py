from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from redpanda.assistant.builtin_tools import build_builtin_tools
from redpanda.assistant.context.projection import ModelContextSettings
from redpanda.assistant.decision import bind_executor_tools
from redpanda.filesystem.registry import WorkspaceRecord
from redpanda.filesystem.workspace import FileWorkspace
from redpanda.runtime import (
    AgentRuntime,
    CommandOutcomeReceived,
    InvokeTool,
    ModelDecision,
)
from tests.assistant.test_runner import ScriptedDecisionMaker
from tests.fixtures.artifacts import MemoryArtifactGateway
from tests.fixtures.memory_journal import MemoryJournal


class ColleagueWorktreeAccessTest(unittest.IsolatedAsyncioTestCase):
    async def test_forbidden_writes_return_denial_without_requesting_authorization(self):
        with TemporaryDirectory() as directory:
            project = Path(directory)
            own, colleague = project / "ada", project / "lin"
            own.mkdir()
            colleague.mkdir()
            target = colleague / "note.txt"
            target.write_text("original", encoding="utf-8")
            runner = await build_builtin_tools(
                WorkspaceRecord("workspace-test", own, False, "2026-10-19"), colleague_roots=(project,),
            )
            bindings = bind_executor_tools(runner, MemoryArtifactGateway(), ModelContextSettings())
            for name, extra in (
                ("write_file", {"content": "changed", "overwrite": True}),
                ("apply_patch", {"old_block": "original", "new_block": "changed"}),
                ("replace_all", {"old_block": "original", "new_block": "changed"}),
            ):
                with self.subTest(tool=name):
                    arguments = {"path": str(target), **extra}
                    runtime = AgentRuntime(MemoryJournal(), ScriptedDecisionMaker((
                        lambda _, name=name, arguments=arguments: ModelDecision(command_requests=(
                            InvokeTool(name, tuple(arguments.items())),
                        )),
                    )), bindings)
                    await runtime.receive_user_message("s", "write", delivery_id="write")
                    try:
                        # A frozen rejection must never invoke a writer, even if
                        # the path/permissions change before dispatch or recovery.
                        with patch.object(runner._executor, "execute_parsed", new_callable=AsyncMock) as execute:
                            issued = await runtime.advance("s")
                            self.assertFalse(issued.step.commands[0].requires_authorization)
                            while runtime.dispatcher.active_count:
                                await asyncio.sleep(0)
                            execute.assert_not_awaited()
                        outcomes = [e.payload for e in await runtime.snapshot("s")
                                    if isinstance(e.payload, CommandOutcomeReceived)]
                        self.assertEqual(outcomes[0].outcome.value["code"], "ENVIRONMENT_PERMISSION_DENIED")
                        self.assertEqual(target.read_text(encoding="utf-8"), "original")
                        self.assertTrue(runner.requires_authorization(name)({"path": str(own / "note.txt")}))
                    finally:
                        await runtime.dispatcher.close()

    async def test_full_access_can_write_another_project_but_not_same_project_colleagues(self):
        with TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            own, colleague = project / "ada", project / "lin"
            own.mkdir(parents=True)
            colleague.mkdir()
            other = Path(directory) / "other-project" / "agent" / "note.txt"
            runner = await build_builtin_tools(
                WorkspaceRecord("workspace-test", own, True, "2026-10-19"), colleague_roots=(project,),
            )
            denied = await runner.execute("write_file", {"path": str(colleague / "note.txt"), "content": "mine"})
            self.assertEqual(denied["code"], "ENVIRONMENT_PERMISSION_DENIED")
            allowed = await runner.execute("write_file", {"path": str(other), "content": "mine"})
            self.assertTrue(allowed["ok"])
            self.assertEqual(other.read_text(encoding="utf-8"), "mine")

    async def test_colleague_worktrees_are_readable_and_own_worktree_stays_writable(self):
        with TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            personal = Path(directory) / "bots"
            own, colleague = personal / "ada", project / "lin"
            own.mkdir(parents=True)
            runner = await build_builtin_tools(
                FileWorkspace(own), colleague_roots=(personal, project),
            )
            # A code colleague created after this Bot started is immediately readable.
            colleague.mkdir()
            (colleague / "draft.py").write_text("half done", encoding="utf-8")

            read = await runner.execute("read_file", {"path": str(colleague / "draft.py")})
            self.assertTrue(read["ok"])
            self.assertIn("half done", str(read["data"]))
            write = await runner.execute("write_file", {"path": str(colleague / "draft.py"),
                                                        "content": "mine", "overwrite": True})
            self.assertEqual(write["code"], "ENVIRONMENT_PERMISSION_DENIED")
            self.assertEqual((colleague / "draft.py").read_text(encoding="utf-8"), "half done")
            own_write = await runner.execute("write_file", {"path": str(own / "note.py"), "content": "mine"})
            self.assertTrue(own_write["ok"])
