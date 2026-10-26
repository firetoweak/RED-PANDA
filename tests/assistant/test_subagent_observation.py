from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from konden.assistant.conversations import AssistantQueries
from konden.assistant.host.session_store import SessionStore
from konden.assistant.host.supervisor import HostSupervisor
from konden.assistant.runner import SessionNotFoundError
from konden.assistant.subagent.subagent import DELEGATE, child_session_id, persist_return, return_data
from konden.channels.web.channel import WebChannel
from konden.paths import KondenHome
from konden.runtime import AgentRuntime, InvokeTool, ModelDecision, SqliteJournal, ToolBinding
from tests.assistant.test_runner import ScriptedDecisionMaker
from tests.session_scheduler import settle_session


class SubagentObservationTest(unittest.IsolatedAsyncioTestCase):
    async def test_summary_lists_old_and_failed_delegations_without_waking_or_selecting(self):
        with TemporaryDirectory() as directory:
            home = KondenHome(Path(directory) / "home")
            store = SessionStore(home.runtime_sessions_root)
            await store.create("parent")
            tasks = ("创建中", "后台执行", "等待消息", "已结束", "子执行失败", "创建失败")

            async def delegate(_context, arguments):
                if arguments["task"] == "创建失败":
                    return {"ok": False, "code": "CREATE_FAILED", "error": "未能创建子任务"}
                return {"ok": True, "code": "DELEGATED"}

            parent = AgentRuntime(store.journal("parent"), ScriptedDecisionMaker((
                lambda _: ModelDecision(command_requests=tuple(
                    InvokeTool(DELEGATE, (("task", task),)) for task in tasks
                )),
                lambda _: ModelDecision(content="稍后的普通对话"),
            )), {DELEGATE: ToolBinding(delegate, decision_on_outcome=False)})
            await parent.receive_user_message("parent", "委派任务", delivery_id="tasks")
            first = await settle_session(parent, "parent")
            commands = first.state.steps[0].commands
            ids = [child_session_id("parent", command.command.command_id) for command in commands]
            for child_id in ids[1:-1]:
                await store.create(child_id)
            for child_id, failure in ((ids[3], None), (ids[4], "执行失败")):
                await persist_return(store.journal(child_id), child_id, return_data(
                    child_id, failure=failure, reason="协作结束",
                ))
            await parent.receive_user_message("parent", "插话", delivery_id="later")
            await settle_session(parent, "parent")
            before = {sid: await store.journal(sid).snapshot(sid) for sid in ("parent", *ids[1:-1])}
            host = HostSupervisor(store, None, home, None, llm=None)
            self.addAsyncCleanup(host.close)
            channel = WebChannel(host, None)

            async def background_commands(session_id):
                return [{"state": "running"}] if session_id == ids[1] else []

            with (
                patch.object(host.commands.manager, "list", side_effect=background_commands),
                patch.object(host, "request", new=AsyncMock()) as request,
                patch.object(host, "select", new=AsyncMock()) as select,
            ):
                summaries = await channel.list_subagents("parent")
                self.assertEqual(summaries, [
                    {"command_id": command.command.command_id, "child_session_id": child_id,
                     "task": task, "status": status}
                    for command, child_id, task, status in zip(commands, ids, tasks, (
                        "creating", "running", "waiting", "closed", "failed", "failed",
                    ), strict=True)
                ])
                legacy = await host._list_subagents("parent")
                self.assertEqual(legacy, {"ok": True, "code": "SUBAGENTS_READ", "data": {
                    "subagents": [{key: value for key, value in child.items() if key != "command_id"}
                                  for child in summaries],
                }})
                request.assert_not_awaited()
                select.assert_not_awaited()
            self.assertFalse(host.workers)
            self.assertFalse(host.selections)
            self.assertEqual({sid: await store.journal(sid).snapshot(sid) for sid in before}, before)

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
                activity=lambda _: "idle", auto_authorize=lambda: False,
                current_failure=lambda _: None,
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
                child_id, failure=None, reason="父收回",
            ))
            returned = await queries.observe_subagent("parent", command_id)
            self.assertIsNone(returned.result.failure)
            self.assertEqual(returned.result.reason, "父收回")
            with self.assertRaises(SessionNotFoundError):
                await queries.observe_subagent("parent", "不是委派")
