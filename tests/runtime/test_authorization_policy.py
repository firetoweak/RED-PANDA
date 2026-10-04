import asyncio
import unittest

from redpanda.runtime import InvokeTool, ModelDecision
from redpanda.runtime.dispatcher import ToolBinding
from tests.runtime.test_boundary_slice import (
    RecordingTool,
    ScriptedDecisionMaker,
    runtime_for,
)


class AuthorizationPolicySliceTest(unittest.IsolatedAsyncioTestCase):
    """签发时对 AuthorizationPolicy 求值，结果固化为 Command 上的布尔事实。"""

    SESSION_ID = "policy-session"

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

    def test_rejects_other_types(self):
        async def handler(_context, _arguments):
            return {}

        with self.assertRaises(TypeError):
            ToolBinding(handler, requires_authorization="yes")
