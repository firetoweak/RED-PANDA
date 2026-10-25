from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from konden.assistant.context.projection import ModelContextProjector, outcome_text
from tests.fixtures.artifacts import MemoryArtifactGateway
from konden.assistant.control import AssistantControlPlane
from konden.assistant.decision import with_command_interrupts
from konden.assistant.runner import SessionScheduler
from konden.runtime import (
    AgentRuntime,
    CommandOutcomeReceived,
    InvokeTool,
    ModelDecision,
    RuntimeStatus,
    SqliteJournal,
    StateProjector,
    StepContinuationCancelled,
    ToolBinding,
)
from tests.fixtures.memory_journal import MemoryJournal
from konden.filesystem import FileAccess
from konden.execution.api import CommandEnvironment
from konden.execution.command import CapturedOutput, CommandResult
from konden.filesystem.workspace import (
    FilesystemPermission,
    PermissionBinding,
    RootBinding,
    WorkspaceScope,
    WorkspaceViewSnapshot,
)
from konden.tools.builtin.command_execution import (
    CommandInput,
    create_command_execution_spec,
)
from konden.tools.builtin.command_interrupts import (
    CommandInterrupts,
    run_interruptible,
)
from tests.assistant.test_runner import ScriptedDecisionMaker, SequentialIds
from tests.session_scheduler import SettlingScheduler


def _binding(root: Path, runner) -> tuple[FileAccess, CommandEnvironment]:
    view = WorkspaceViewSnapshot((
        RootBinding("project", WorkspaceScope.TASK, root),
    ))
    files = FileAccess(
        environment_id="local-test",
        workspace_view=view,
        permission_binding=PermissionBinding((
            ("project", FilesystemPermission.READ_WRITE),
        )),
        cwd=root,
    )
    return files, CommandEnvironment(runner, "powershell", "powershell")


def _interrupted(stdout: str) -> CommandResult:
    return CommandResult(
        exit_code=None,
        stdout=CapturedOutput(stdout, len(stdout), False, 0),
        stderr=CapturedOutput("", 0, False, 0),
        duration_ms=1,
        timed_out=False,
        interrupted=True,
    )


# CI runner 偶尔很慢，1 秒等不到命令启动；这里只是等待上限，不影响正常耗时。
STARTUP_TIMEOUT = 10


async def _stop(advancing, scheduler):
    """失败时先停掉推进任务和调度器，再让临时目录被清理。"""
    if not advancing.done():
        advancing.cancel()
    await asyncio.gather(advancing, return_exceptions=True)
    await scheduler.close()


class CancelCommandTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_turn_lets_started_file_write_finish_without_continuing(self):
        started = asyncio.Event()
        finish_write = asyncio.Event()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "written.txt"

        async def write_file(_context, _arguments):
            started.set()
            await finish_write.wait()
            path.write_text("complete", encoding="utf-8")
            return {"ok": True}

        model = ScriptedDecisionMaker((
            lambda _frame: ModelDecision(
                command_requests=(InvokeTool("write_file"),),
            ),
        ))
        runtime = AgentRuntime(
            MemoryJournal(), model, {"write_file": ToolBinding(write_file)},
            SequentialIds(),
        )
        scheduler = SettlingScheduler(runtime, "session")
        scheduler.command_interrupts = CommandInterrupts()
        try:
            await runtime.receive_user_message("session", "write", delivery_id="delivery")
            await scheduler.wake("session")
            await asyncio.wait_for(started.wait(), timeout=STARTUP_TIMEOUT)

            await scheduler.cancel_turn("session")
            self.assertFalse(path.exists())
            self.assertFalse(scheduler.idle)

            finish_write.set()
            await asyncio.wait_for(scheduler.join(), timeout=STARTUP_TIMEOUT)
            self.assertEqual(path.read_text(encoding="utf-8"), "complete")
            state = await runtime.state("session")
            self.assertEqual(state.commands[0].outcome.value, {"ok": True})
            self.assertEqual(state.status, RuntimeStatus.WAITING)
            self.assertEqual(len(model.frames), 1)
        finally:
            finish_write.set()
            await scheduler.close()

    async def test_cancel_turn_interrupts_every_running_execute_command(self):
        started = asyncio.Event()
        running = 0

        class Runner:
            async def run(self, command, cwd, timeout_seconds, *, interrupt=None):
                nonlocal running
                running += 1
                if running == 2:
                    started.set()
                await interrupt.wait()
                return _interrupted(command)

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        interrupts = CommandInterrupts()
        spec = create_command_execution_spec(
            *_binding(root, Runner()), interrupts.current
        )

        async def handler(context, arguments):
            async def execute():
                return await spec.handler(
                    CommandInput.model_validate(arguments)
                )

            return await run_interruptible(
                interrupts,
                session_id=context.session_id,
                command_id=context.command_id,
                attempt_id=context.attempt_id,
                execute=execute,
            )

        runtime = AgentRuntime(
            SqliteJournal(root / "journal.sqlite"),
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(
                    command_requests=(
                        InvokeTool("execute_command", (("command", "one"),)),
                        InvokeTool("execute_command", (("command", "two"),)),
                    ),
                ),
            )),
            {"execute_command": ToolBinding(handler)},
            SequentialIds(),
        )
        scheduler = SessionScheduler(
            runtime,
            "session",
            control=AssistantControlPlane(()),
        )
        scheduler.command_interrupts = interrupts
        await runtime.receive_user_message("session", "run", delivery_id="delivery")
        advancing = asyncio.create_task(runtime.advance("session"))
        self.addAsyncCleanup(_stop, advancing, scheduler)
        await asyncio.wait_for(started.wait(), timeout=STARTUP_TIMEOUT)

        await scheduler.cancel_turn("session")
        await advancing
        async with asyncio.timeout(STARTUP_TIMEOUT):
            while not scheduler.idle:
                await asyncio.sleep(0)

        self.assertIsNone(scheduler._failure)
        self.assertTrue(scheduler.idle)
        state = await runtime.state("session")
        self.assertEqual(state.status, RuntimeStatus.WAITING)
        self.assertEqual(len(state.steps), 1)
        codes = sorted(
            command.outcome.value["code"] for command in state.commands
        )
        self.assertEqual(codes, ["COMMAND_INTERRUPTED", "COMMAND_INTERRUPTED"])
        for command in state.commands:
            value = command.outcome.value
            self.assertIsNone(value["ok"])
            self.assertIs(value["data"]["output_is_result"], False)
            self.assertIn(value["data"]["stdout"]["content"], {"one", "two"})
            projected = outcome_text(command.outcome)
            self.assertIn('"code": "COMMAND_INTERRUPTED"', projected)
            self.assertIn('"output_is_result": false', projected)
            self.assertIn("用户停止", projected)
            self.assertIn("可能已部分执行", projected)
            self.assertIn("先检查相关文件和外部状态", projected)
            self.assertNotIn("COMMAND_TIMEOUT", projected)
        events = await runtime.snapshot("session")
        self.assertEqual(
            sum(
                isinstance(event.payload, StepContinuationCancelled)
                for event in events
            ),
            1,
        )
        self.assertEqual(
            sum(
                isinstance(event.payload, CommandOutcomeReceived)
                for event in events
            ),
            2,
        )

        await runtime.receive_user_message("session", "继续", delivery_id="continue")
        restored_events = await SqliteJournal(root / "journal.sqlite").snapshot("session")
        prepared = ModelContextProjector(gateway=MemoryArtifactGateway()).prepare(
            restored_events, StateProjector().project_visible("session", restored_events),
            "session",
        )
        restored_results = [
            json.loads(message["content"])
            for message in prepared.messages if message["role"] == "tool"
        ]
        self.assertEqual(restored_results, [
            json.loads(outcome_text(command.outcome)) for command in state.commands
        ])

    async def test_cancel_turn_reaches_a_command_still_queued_behind_another(self):
        # Managed commands serialize on the file operation; the second one waits outside it.
        started = asyncio.Event()
        serialized = asyncio.Lock()

        class Runner:
            async def run(self, command, cwd, timeout_seconds, *, interrupt=None):
                started.set()
                await interrupt.wait()
                return _interrupted(command)

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        interrupts = CommandInterrupts()
        spec = create_command_execution_spec(*_binding(root, Runner()), interrupts.current)

        async def queued(_context, arguments):
            async with serialized:
                return await spec.handler(CommandInput.model_validate(arguments))

        runtime = AgentRuntime(
            SqliteJournal(root / "journal.sqlite"),
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(command_requests=(
                    InvokeTool("execute_command", (("command", "one"),)),
                    InvokeTool("execute_command", (("command", "two"),)),
                )),
            )),
            {"execute_command": with_command_interrupts(ToolBinding(queued), interrupts)},
            SequentialIds(),
        )
        scheduler = SessionScheduler(runtime, "session", control=AssistantControlPlane(()))
        scheduler.command_interrupts = interrupts
        await runtime.receive_user_message("session", "run", delivery_id="delivery")
        advancing = asyncio.create_task(runtime.advance("session"))
        self.addAsyncCleanup(_stop, advancing, scheduler)
        await asyncio.wait_for(started.wait(), timeout=STARTUP_TIMEOUT)

        await asyncio.wait_for(scheduler.cancel_turn("session"), timeout=STARTUP_TIMEOUT)
        await advancing

        state = await runtime.state("session")
        # A queued command that missed the stop would still be running here: no outcome yet.
        self.assertEqual(
            [None if command.outcome is None else command.outcome.value["code"]
             for command in state.commands],
            ["COMMAND_INTERRUPTED", "COMMAND_INTERRUPTED"],
        )

    async def test_kill_failure_is_exposed_and_does_not_record_an_outcome(self):
        started = asyncio.Event()

        class Runner:
            async def run(self, command, cwd, timeout_seconds, *, interrupt=None):
                started.set()
                await interrupt.wait()
                raise RuntimeError("kill failed")

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        interrupts = CommandInterrupts()
        spec = create_command_execution_spec(
            *_binding(root, Runner()), interrupts.current
        )

        async def handler(context, arguments):
            async def execute():
                return await spec.handler(
                    CommandInput.model_validate(arguments)
                )

            return await run_interruptible(
                interrupts,
                session_id=context.session_id,
                command_id=context.command_id,
                attempt_id=context.attempt_id,
                execute=execute,
            )

        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(
                    command_requests=(
                        InvokeTool("execute_command", (("command", "sleep"),)),
                    ),
                ),
            )),
            {"execute_command": ToolBinding(handler)},
            SequentialIds(),
        )
        scheduler = SessionScheduler(
            runtime,
            "session",
            control=AssistantControlPlane(()),
        )
        scheduler.command_interrupts = interrupts
        await runtime.receive_user_message("session", "run", delivery_id="delivery")
        advancing = asyncio.create_task(runtime.advance("session"))
        self.addAsyncCleanup(_stop, advancing, scheduler)
        await asyncio.wait_for(started.wait(), timeout=STARTUP_TIMEOUT)

        with self.assertRaisesRegex(RuntimeError, "kill failed"):
            await scheduler.cancel_turn("session")
        await advancing

        events = await runtime.snapshot("session")
        self.assertFalse(
            any(isinstance(event.payload, CommandOutcomeReceived) for event in events)
        )
