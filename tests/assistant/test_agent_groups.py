from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from redpanda.assistant.agent_messages import SEND_GROUP_MESSAGE
from redpanda.assistant.agents import (
    GROUP_ATTENTION, GROUP_CREATED, GROUP_MESSAGE, AgentCreated, fact_arguments, parse_agent_fact,
)
from redpanda.assistant.host.agent_groups import GroupNameTaken
from redpanda.assistant.host.agent_roster import AgentNotFound
from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.filesystem.registry import WorkspaceRegistry
from redpanda.paths import RedPandaHome
from redpanda.runtime import AgentRuntime, DomainFactCommitted, InvokeTool, ModelDecision, SqliteJournal, ToolBinding
from tests.assistant.test_runner import ScriptedDecisionMaker


MEMBERS = ("Ada", "Lin", "Third")


class AgentGroupsTest(unittest.IsolatedAsyncioTestCase):
    """Worker quiescence is replaced by a turn script; real Workers are covered in process."""

    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.home = RedPandaHome(self.root / "home")
        self.store = SessionStore(self.home.runtime_sessions_root)
        registry = WorkspaceRegistry.load(self.home.workspaces_path)
        project, other = self.root / "project", self.root / "other"
        project.mkdir()
        other.mkdir()
        self.workspace, self.other = registry.register_path(project), registry.register_path(other)
        self.host = HostSupervisor(self.store, None, self.home, None, llm=None, workspaces=registry)
        for name, workspace in (*((name, self.workspace) for name in MEMBERS), ("Elsewhere", self.other)):
            await self.store.create(name, workspace_id=workspace.workspace_id, initial_fact=fact_arguments(
                AgentCreated(name=name, workspace_id=workspace.workspace_id,
                             worktree=str(project), branch=f"agent/{name}"), delivery_id="created"))
            # Each Agent has already introduced itself; later decisions answer group facts.
            await AgentRuntime(self.journal(name), ScriptedDecisionMaker((lambda _: ModelDecision(content="你好"),)),
                               {}).advance(name)

        async def deliver(operation, session_id, arguments):
            self.assertEqual(operation, "fact")
            await AgentRuntime(self.journal(session_id), None, {}).receive_domain_fact(session_id, **arguments)

        self.calls = []
        self.speeches = {}
        self.work = []
        for target, mock in (("request", AsyncMock(side_effect=deliver)),
                             ("wait_quiescent", AsyncMock(side_effect=self.turn)),
                             ("cancel_turn", AsyncMock())):
            patcher = patch.object(self.host, target, mock)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cancel_turn = self.host.cancel_turn

    async def asyncTearDown(self):
        await self.host.close()
        self.directory.cleanup()

    def journal(self, session_id):
        return SqliteJournal(self.store.require(session_id))

    async def facts(self, session_id, fact_type):
        return [event.payload for event in await self.journal(session_id).snapshot(session_id)
                if isinstance(event.payload, DomainFactCommitted) and event.payload.fact_type == fact_type]

    async def speak(self, name, *speeches):
        """The called member decides on its attention and sends each speech."""
        runtime = AgentRuntime(self.journal(name), ScriptedDecisionMaker((lambda _: ModelDecision(command_requests=tuple(
            InvokeTool(SEND_GROUP_MESSAGE, tuple(speech.items())) for speech in speeches)),)),
            {SEND_GROUP_MESSAGE: ToolBinding(AsyncMock(return_value={"ok": True}))})
        step = (await runtime.advance(name)).step
        results = [await self.host._route(SEND_GROUP_MESSAGE, name, {"command_id": command.command_id,
                   "mention": [], "reply_to": None, **speech})
                   for command, speech in zip(step.commands, speeches)]
        # Settle the commands so a later attention becomes this member's next trigger.
        await runtime.dispatcher.start_pending(name)
        while runtime.dispatcher.active_count:
            await asyncio.sleep(0)
        return results

    async def turn(self, session_id):
        self.calls.append(session_id)
        speeches = self.speeches.get(session_id, list)()
        if speeches:
            # The member works on its own; a speech ends the Host's wait, not the member's work.
            # Members here finish in call order so limit counts are deterministic.
            work = asyncio.ensure_future(self.after(list(self.work), self.speak(session_id, *speeches)))
            self.work.append(work)
            await asyncio.shield(work)

    @staticmethod
    async def after(earlier, work):
        await asyncio.gather(*earlier)
        return await work

    async def group(self, members=MEMBERS, name="开发组"):
        return await self.host.agent_groups.create(workspace_id=self.workspace.workspace_id, name=name,
                                                   members=list(members))

    async def settled(self, group):
        while self.host.agent_groups._drives[group.group_id].running:
            await asyncio.sleep(0.01)
        await asyncio.gather(*self.work)

    async def test_creation_checks_members_and_tells_only_them(self):
        notified = AsyncMock()
        self.host.group_changed_sink = notified
        group = await self.group(("Ada", "Lin"))
        await asyncio.gather(*self.host.watchers)
        notified.assert_awaited_once_with(group.group_id)
        for name, count in (("Ada", 1), ("Lin", 1), ("Third", 0)):
            created = await self.facts(name, GROUP_CREATED)
            self.assertEqual(len(created), count)
            self.assertFalse(any(fact.requests_decision for fact in created))
        self.assertEqual(parse_agent_fact((await self.facts("Ada", GROUP_CREATED))[0]).members, ["Ada", "Lin"])
        with self.assertRaises(GroupNameTaken):
            await self.group(("Ada", "Third"))
        for members, error in ((("Ada", "Nobody"), AgentNotFound), (("Ada", "Elsewhere"), AgentNotFound),
                               (("Ada",), ValidationError), (("Ada", "Ada"), ValidationError)):
            with self.assertRaises(error):
                await self.group(members, name=f"组-{members}")
        self.assertEqual([item.group_id for item in await self.host.agent_groups.list()], [group.group_id])

    async def test_human_mentions_choose_who_is_called_and_in_what_order(self):
        group = await self.group()
        await self.host.agent_groups.post(group.group_id, "请两位看看", ["Third", "Ada"])
        await self.settled(group)
        self.assertEqual(self.calls, ["Third", "Ada"])
        for name in MEMBERS:
            self.assertEqual(len(await self.facts(name, GROUP_MESSAGE)), 1)
            self.assertEqual(len(await self.facts(name, GROUP_ATTENTION)), name != "Lin")
        self.calls.clear()
        await self.host.agent_groups.post(group.group_id, "大家好", [])
        await self.settled(group)
        self.assertEqual(self.calls, list(MEMBERS))
        with self.assertRaisesRegex(ValueError, "不在这个群"):
            await self.host.agent_groups.post(group.group_id, "喂", ["Elsewhere"])

    async def test_speeches_open_rounds_until_silence_or_the_round_limit(self):
        group = await self.group()
        self.speeches = {name: (lambda name=name: [{"group": "开发组", "text": f"{name} 收到"}]) for name in MEMBERS}
        await self.host.agent_groups.post(group.group_id, "大家各说一句", [])
        await self.settled(group)
        # Three rounds, every member called and speaking each round.
        self.assertEqual(len(self.calls), 9)
        self.assertEqual(sum(result["code"] == "DELIVERED" for results in [work.result() for work in self.work] for result in results), 9)
        self.assertIn("轮数上限", [work.result() for work in self.work][-1][0]["data"]["notice"])
        self.assertEqual(len(await self.host.agent_groups.messages(group.group_id)), 10)
        # A speaker never receives its own speech; others receive every one.
        self.assertEqual(len(await self.facts("Ada", GROUP_MESSAGE)), 7)
        self.calls.clear()
        self.speeches = {}
        await self.host.agent_groups.post(group.group_id, "这次不用回", [])
        await self.settled(group)
        self.assertEqual(self.calls, list(MEMBERS))

    async def test_round_and_drive_limits_return_known_results(self):
        group = await self.group(("Ada", "Lin"))
        speech = {"group": "开发组", "text": "补充", "mention": ["Lin"]}
        ada = iter(([speech] * 4,))
        lin = iter(([{"group": "开发组", "text": "收到"}, {"group": "开发组", "text": "再补充"}],))
        self.speeches = {"Ada": lambda: next(ada, []), "Lin": lambda: next(lin, [])}
        with patch("redpanda.assistant.host.agent_groups.MAX_GROUP_REPLIES", 4):
            await self.host.agent_groups.post(group.group_id, "Ada 先说", ["Ada"])
            await self.settled(group)
        # Ada's first speech ends her turn, so round two (Lin) starts while she keeps speaking:
        # her later speeches count in round two, where the per-round cap stops the fourth.
        self.assertEqual([[result["code"] for result in work.result()] for work in self.work],
                         [["DELIVERED", "DELIVERED", "DELIVERED", "SPEECH_LIMIT_REACHED"],
                          ["DELIVERED", "GROUP_LIMIT_REACHED"]])
        self.assertEqual(self.calls, ["Ada", "Lin", "Lin", "Ada"])

    async def test_observers_hear_when_the_drive_ends(self):
        group = await self.group(("Ada", "Lin"))
        seen = []

        async def changed(group_id):
            view = await self.host.agent_groups.view(group_id)
            seen.append((view["active"], view["calling"], len(view["messages"])))

        self.host.group_changed_sink = changed
        await self.host.agent_groups.post(group.group_id, "大家好", [])
        await self.settled(group)
        await asyncio.gather(*self.host.watchers)
        # The last notice comes after the drive ended, so a page reading it is never left "calling".
        self.assertEqual(seen[-1], (False, [], 1))

    async def test_stop_interrupts_the_called_member_and_calls_no_one_else(self):
        group = await self.group()

        called = asyncio.Event()

        async def busy(session_id):
            self.calls.append(session_id)
            called.set()
            await asyncio.Event().wait()

        self.host.wait_quiescent.side_effect = busy
        await self.host.agent_groups.post(group.group_id, "大家看看", [])
        await called.wait()
        await self.host.agent_groups.stop(group.group_id)
        await self.settled(group)
        self.assertEqual(self.calls, ["Ada"])
        self.cancel_turn.assert_awaited_once_with("Ada")

    async def test_agent_speech_is_idempotent_and_checks_its_addressing(self):
        group = await self.group(("Ada", "Lin"))
        await self.host.agent_groups.post(group.group_id, "Ada 说说", ["Ada"])
        await self.settled(group)
        commands = {}
        for name in ("Ada", "Third"):
            runtime = AgentRuntime(self.journal(name), ScriptedDecisionMaker((lambda _: ModelDecision(
                command_requests=(InvokeTool(SEND_GROUP_MESSAGE, (("group", "开发组"), ("text", "好"))),)),)),
                {SEND_GROUP_MESSAGE: ToolBinding(AsyncMock())})
            if name == "Third":
                await runtime.receive_user_message(name, "去群里说一声", delivery_id="human")
            commands[name] = (await runtime.advance(name)).step.commands[0].command_id
        send = lambda name="Ada", **extra: self.host._route(SEND_GROUP_MESSAGE, name, {
            "command_id": commands[name], "group": "开发组", "text": "好", "mention": [], "reply_to": None, **extra})
        for extra, code in (({"group": "测试组"}, "GROUP_NOT_FOUND"), ({"mention": ["Ada"]}, "INVALID_MENTION"),
                            ({"mention": ["Third"]}, "INVALID_MENTION"), ({"reply_to": "missing"}, "INVALID_REPLY")):
            self.assertEqual((await send(**extra))["code"], code)
        self.assertEqual((await send("Third"))["code"], "NOT_A_MEMBER")
        first, again = await send(), await send()
        self.assertEqual(first, again)
        await self.settled(group)
        self.assertEqual(len(await self.host.agent_groups.messages(group.group_id)), 2)
        self.assertEqual(len(await self.facts("Lin", GROUP_MESSAGE)), 2)
        with self.assertRaisesRegex(ValueError, "conflicts"):
            await send(text="改了")
