from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from helperme.assistant.conversations import AssistantQueries
from helperme.assistant.host.session_store import SessionStore
from helperme.assistant.runner import SessionNotFoundError
from helperme.assistant.subagent.subagent import DELEGATE, child_session_id, persist_return, return_data
from helperme.runtime import AgentRuntime, InvokeTool, ModelDecision, SqliteJournal, ToolBinding
from tests.assistant.test_runner import ScriptedDecisionMaker


class SubagentObservationTest(unittest.IsolatedAsyncioTestCase):
    async def test_observation_reads_delegation_and_child_history_without_waking_either(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            await store.create("parent", workspace_id="workspace")
            async def delegate(_context, _arguments):
                return "派出"
            parent = AgentRuntime(SqliteJournal(store.require("parent")), ScriptedDecisionMaker((
                lambda _: ModelDecision(command_requests=(InvokeTool(DELEGATE, (("task", "调查问题"),)),)),
            )), {DELEGATE: ToolBinding(delegate, requires_authorization=True)})
            await parent.receive_user_message("parent", "调查", delivery_id="user")
            advance = await parent.advance("parent")
            command_id = advance.step.commands[0].command_id
            child_id = child_session_id("parent", command_id)
            # 查询端口没有 select / wake / request，观察必须只读。
            host = SimpleNamespace(
                activity=lambda _: "idle", auto_authorize=lambda _: False, is_paused=lambda _: False,
                conversation_status=lambda _: SimpleNamespace(compact_count=0, compact_phase=None),
                next_scheduled_check=lambda _: None,
            )
            queries = AssistantQueries(store, host)
            pending = await queries.observe_subagent("parent", command_id)
            self.assertEqual(pending.session_id, child_id)
            self.assertIsNone(pending.conversation)
            await store.create(child_id, workspace_id="workspace")
            child = AgentRuntime(SqliteJournal(store.require(child_id)), ScriptedDecisionMaker((
                lambda _: ModelDecision(content="调查过程"),
            )), {})
            await child.receive_user_message(child_id, "调查问题", delivery_id="task")
            await child.advance(child_id)
            before_parent = await parent.snapshot("parent")
            before_child = await child.snapshot(child_id)
            observation = await queries.observe_subagent("parent", command_id)
            self.assertEqual(observation.task, "调查问题")
            self.assertEqual(observation.conversation.items[-1].text, "调查过程")
            self.assertIsNone(observation.result)
            self.assertEqual(await parent.snapshot("parent"), before_parent)
            self.assertEqual(await child.snapshot(child_id), before_child)
            await persist_return(SqliteJournal(store.require(child_id)), child_id, return_data(
                child_id, reported=False, summary=None, failure=None, cancelled=True, reason="父收回",
            ))
            returned = await queries.observe_subagent("parent", command_id)
            self.assertTrue(returned.result.cancelled)
            self.assertEqual(returned.result.reason, "父收回")
            with self.assertRaises(SessionNotFoundError):
                await queries.observe_subagent("parent", "不是委派")
