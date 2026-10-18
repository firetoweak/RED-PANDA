import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from redpanda.runtime import (
    CommandOutcomeReceived,
    InvokeTool,
    ModelDecision,
    RuntimeStatus,
    SqliteJournal,
)
from redpanda.runtime.dispatcher import DispatchStartResult, ToolBinding
from redpanda.runtime.model import rejected_tool_arguments
from tests.assistant.test_runner import ScriptedDecisionMaker
from tests.runtime.test_boundary_slice import RecordingTool, runtime_for


class AuthorizationPolicySliceTest(unittest.IsolatedAsyncioTestCase):
    """签发时对 AuthorizationPolicy 求值，结果固化为 Command 上的布尔事实。"""

    SESSION_ID = "policy-session"

    async def test_rejected_target_stays_rejected_after_restart_even_if_policy_now_allows_it(self):
        with TemporaryDirectory() as directory:
            journal = SqliteJournal(Path(directory) / "journal.sqlite")
            tool = RecordingTool("write", requires_authorization=lambda _: rejected_tool_arguments(
                '{"path":"colleague/note.txt"}', code="ENVIRONMENT_PERMISSION_DENIED",
                error="目标只读", hint="选择可写目标。",
            ))
            runtime = runtime_for(tool, ScriptedDecisionMaker((
                lambda _: ModelDecision(command_requests=(InvokeTool("write", (("path", "colleague/note.txt"),)),)),
            )), journal)
            await runtime.receive_user_message(self.SESSION_ID, "write", delivery_id="write")
            # Restart after committing the rejection, before its result is dispatched.
            with patch.object(runtime.dispatcher, "start_pending", new_callable=AsyncMock,
                              return_value=DispatchStartResult((), RuntimeStatus.RUNNABLE)):
                issued = await runtime.advance(self.SESSION_ID)
            await runtime.dispatcher.close()
            tool.requires_authorization = False
            tool.release.set()
            recovered = runtime_for(tool, ScriptedDecisionMaker(()), journal)
            try:
                await recovered.dispatcher.start_pending(self.SESSION_ID)
                while recovered.dispatcher.active_count:
                    await asyncio.sleep(0)
                events = await recovered.snapshot(self.SESSION_ID)
                outcomes = [e.payload for e in events if isinstance(e.payload, CommandOutcomeReceived)]
                self.assertEqual(len(outcomes), 1)
                self.assertEqual(outcomes[0].command_id, issued.step.commands[0].command_id)
                self.assertEqual(outcomes[0].outcome.value["code"], "ENVIRONMENT_PERMISSION_DENIED")
                self.assertEqual(tool.executions, [])
            finally:
                await recovered.dispatcher.close()

    async def test_policy_is_evaluated_per_command_with_arguments(self):
        tool = RecordingTool(
            "shell",
            requires_authorization=lambda args: args.get("dangerous") is True,
        )
        runtime = runtime_for(
            tool,
            ScriptedDecisionMaker(
                (
                    lambda _frame: ModelDecision(
                        command_requests=(
                            InvokeTool("shell", (("dangerous", True),)),
                            InvokeTool("shell", (("dangerous", False),)),
                        ),
                    ),
                )
            ),
        )

        await runtime.receive_user_message(
            self.SESSION_ID,
            "go",
            delivery_id="ask-1",
        )
        advance = await runtime.advance(self.SESSION_ID)

        dangerous, safe = advance.step.commands
        self.assertTrue(dangerous.requires_authorization)
        self.assertFalse(safe.requires_authorization)
        state = await runtime.state(self.SESSION_ID)
        self.assertIsNone(
            state.command(dangerous.command_id).dispatch_eligible_by_event_id
        )
        self.assertEqual(state.command(dangerous.command_id).attempts, ())
        # safe 命令无需授权，在 advance 内被立即认领（认领后 eligible 置 None）。
        self.assertEqual(len(state.command(safe.command_id).attempts), 1)

        await runtime.grant_command(self.SESSION_ID, dangerous.command_id)
        tool.release.set()
        while runtime.dispatcher.active_count:
            await asyncio.sleep(0)


class ToolBindingAuthorizationValidationTest(unittest.TestCase):
    def test_accepts_bool_or_callable(self):
        async def handler(_context, _arguments):
            return {}

        self.assertTrue(
            ToolBinding(handler, requires_authorization=True).requires_authorization
        )
        policy = lambda _args: True  # noqa: E731
        self.assertIs(
            ToolBinding(handler, requires_authorization=policy).requires_authorization,
            policy,
        )

