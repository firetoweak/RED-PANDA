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
from redpanda.assistant.host.agent_groups import AgentGroups, GroupDissolvedError, GroupNameTaken
from redpanda.assistant.host.agent_roster import AgentNotFound
from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.filesystem.registry import WorkspaceRegistry
from redpanda.paths import RedPandaHome
from redpanda.runtime import AgentRuntime, DomainFactCommitted, InvokeTool, ModelDecision, SqliteJournal, ToolBinding
from tests.assistant.test_runner import ScriptedDecisionMaker


MEMBERS = ("Ada", "Lin", "Third")
# A scripted speech quoting the message its speaker was last called to answer.
ANSWER = object()


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
            await self.store.create(name, directory=project, initial_fact=fact_arguments(
                AgentCreated(name=name, directory=str(project)), delivery_id="created"))
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
                             ("wait_quiescent", AsyncMock(side_effect=self.turn))):
            patcher = patch.object(self.host, target, mock)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        await self.host.close()
        self.directory.cleanup()

    def journal(self, session_id):
        return SqliteJournal(self.store.require(session_id))

    async def test_dissolve_preserves_history_and_does_not_resume_the_pending_calls(self):
        group = await self.host.agent_groups.create(name="开发群", members=["Ada", "Lin"])
        called = asyncio.Event()

        async def working(_session_id):
            called.set()
            await asyncio.Event().wait()

        self.host.wait_quiescent.side_effect = working
        await self.host.agent_groups.post(group.group_id, "请讨论", [])
        await asyncio.wait_for(called.wait(), 2)
        await self.host.agent_groups.dissolve(group.group_id)
        await self.host.agent_groups.dissolve(group.group_id)
        await self.settled(group)
        recovered = self.host.agent_groups = AgentGroups(self.host)
        await recovered.recover()
        self.assertEqual(await recovered.list(), ())
        view = await recovered.view(group.group_id)
        self.assertTrue(view["dissolved"])
        self.assertEqual(view["calling"], [])
        self.assertEqual([message["text"] for message in view["messages"]], ["请讨论"])
        self.assertEqual(len(await self.facts("Lin", GROUP_ATTENTION)), 0)
        with self.assertRaises(GroupDissolvedError):
            await recovered.post(group.group_id, "还在吗", [])

    async def test_deleted_member_is_not_called_and_surviving_member_still_participates(self):
        group = await self.host.agent_groups.create(name="开发群", members=["Ada", "Lin"])
        await self.host.delete_agent("Ada")
        self.assertEqual((await self.host.agent_groups.view(group.group_id))["members"], ["Lin"])
        await self.host.agent_groups.post(group.group_id, "请继续", [])
        await self.settled(group)
        self.assertEqual(len(await self.facts("Ada", GROUP_ATTENTION)), 0)
        self.assertEqual(len(await self.facts("Lin", GROUP_ATTENTION)), 1)

    async def facts(self, session_id, fact_type):
        return [event.payload for event in await self.journal(session_id).snapshot(session_id)
                if isinstance(event.payload, DomainFactCommitted) and event.payload.fact_type == fact_type]

    async def speak(self, name, *speeches):
        """The called member decides on its attention and sends each speech."""
        if any(speech.get("reply_to") is ANSWER for speech in speeches):
            called = parse_agent_fact((await self.facts(name, GROUP_ATTENTION))[-1]).message_id
            speeches = tuple({**speech, "reply_to": called} if speech.get("reply_to") is ANSWER else speech
                             for speech in speeches)
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
        return await self.host.agent_groups.create(name=name,
                                                   members=list(members))

    async def settled(self, group):
        # A speech finishing after its call ended can start the drive again.
        while True:
            while self.host.agent_groups._drives[group.group_id].running:
                await asyncio.sleep(0.01)
            count = len(self.work)
            await asyncio.gather(*self.work)
            if not self.host.agent_groups._drives[group.group_id].running and len(self.work) == count:
                return

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
        for members, error in ((("Ada", "Nobody"), AgentNotFound),
                               (("Ada",), ValidationError), (("Ada", "Ada"), ValidationError)):
            with self.assertRaises(error):
                await self.group(members, name=f"组-{members}")
        self.assertEqual([item.group_id for item in await self.host.agent_groups.list()], [group.group_id])

    async def test_rename_preserves_queued_group_turn_and_current_mentions_after_restart(self):
        group = await self.group(("Ada", "Lin"))
        entered, release = asyncio.Event(), asyncio.Event()

        async def working(session_id):
            self.calls.append(session_id)
            if session_id == "Ada":
                entered.set()
                await release.wait()

        self.host.wait_quiescent.side_effect = working
        await self.host.agent_groups.post(group.group_id, "大家看看", [])
        await entered.wait()
        await self.host.rename_agent("Lin", "小林")
        self.assertEqual((await self.host.agent_groups.view(group.group_id))["members"], ["Ada", "小林"])
        release.set()
        await self.settled(group)
        self.assertEqual(self.calls, ["Ada", "Lin"])
        self.host.agent_groups = AgentGroups(self.host)
        await self.host.agent_groups.recover()
        await self.settled(group)
        self.assertEqual(self.calls, ["Ada", "Lin"])
        await self.host.agent_groups.post(group.group_id, "请小林回复", ["小林"])
        await self.settled(group)
        self.assertEqual(self.calls, ["Ada", "Lin", "Lin"])

    async def test_restart_completes_partial_delivery_and_preserves_human_request_identity(self):
        group = await self.group()
        deliver = self.host.request.side_effect

        async def lost(operation, session_id, arguments):
            if session_id == "Lin" and arguments["fact_type"] == GROUP_MESSAGE:
                raise EOFError("recipient unavailable")
            return await deliver(operation, session_id, arguments)

        self.host.request.side_effect = lost
        with self.assertRaises(EOFError):
            await self.host.agent_groups.post(group.group_id, "大家看看", [], delivery_id="original")
        # Let successful recipients finish before replacing the Host-owned service.
        await asyncio.sleep(.05)
        self.host.request.side_effect = deliver
        self.host.agent_groups = AgentGroups(self.host)
        await self.host.agent_groups.recover()
        await self.settled(group)
        self.assertEqual(self.calls, list(MEMBERS))
        await self.host.agent_groups.post(group.group_id, "大家看看", [], delivery_id="original")
        await self.settled(group)
        self.assertEqual(self.calls, list(MEMBERS))
        self.assertEqual(len(await self.host.agent_groups.messages(group.group_id)), 1)
        for name in MEMBERS:
            self.assertEqual(len(await self.facts(name, GROUP_MESSAGE)), 1)
            self.assertEqual(len(await self.facts(name, GROUP_ATTENTION)), 1)
        with self.assertRaisesRegex(ValueError, "conflicts"):
            await self.host.agent_groups.post(group.group_id, "改过的正文", [], delivery_id="original")

    async def test_restart_preserves_waiting_batch_without_recalling_completed_members(self):
        group = await self.group()
        original = self.host.agent_groups
        call = original._call

        async def die_before_lin(drive, name, request):
            if name == "Lin":
                raise EOFError("Host exited")
            await call(drive, name, request)

        with patch.object(original, "_call", side_effect=die_before_lin):
            await original.post(group.group_id, "大家看看", [])
            await self.settled(group)
        self.assertEqual(self.calls, ["Ada"])
        self.host.agent_groups = AgentGroups(self.host)
        await self.host.agent_groups.recover()
        await self.settled(group)
        self.assertEqual(self.calls, list(MEMBERS))
        self.host.agent_groups = AgentGroups(self.host)
        await self.host.agent_groups.recover()
        await self.settled(group)
        self.assertEqual(self.calls, list(MEMBERS))

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
        self.speeches = {name: (lambda name=name: [{"group": "开发组", "text": f"{name} 收到", "reply_to": ANSWER}]) for name in MEMBERS}
        await self.host.agent_groups.post(group.group_id, "大家各说一句", [])
        await self.settled(group)
        # Three rounds, every member called and speaking each round.
        self.assertEqual(len(self.calls), 9)
        self.assertEqual(sum(result["code"] == "DELIVERED" for results in [work.result() for work in self.work] for result in results), 9)
        self.assertIn("轮数上限", [work.result() for work in self.work][-1][0]["data"]["notice"])
        self.assertEqual(len(await self.host.agent_groups.messages(group.group_id)), 10)
        # A speaker never receives its own speech; others receive every one.
        self.assertEqual(len(await self.facts("Ada", GROUP_MESSAGE)), 7)
        last = (await self.host.agent_groups.messages(group.group_id))[-1]
        original = self.work[-1].result()[0]
        self.host.agent_groups = AgentGroups(self.host)
        await self.host.agent_groups.recover()
        await self.settled(group)
        repeated = await self.host.agent_groups.send(last.sender.name, {
            "group": last.group, "text": last.text, "mention": last.mentioned_agents,
            "reply_to": last.reply_to, "message_id": last.message_id}, last.origin, AsyncMock())
        self.assertEqual(repeated, original)
        self.assertEqual(len(self.calls), 9)
        self.calls.clear()
        self.speeches = {}
        await self.host.agent_groups.post(group.group_id, "这次不用回", [])
        await self.settled(group)
        self.assertEqual(self.calls, list(MEMBERS))

    async def test_reply_limit_returns_a_known_result(self):
        group = await self.group(("Ada", "Lin"))
        ada = iter(([{"group": "开发组", "text": "补充", "mention": ["Lin"], "reply_to": ANSWER}],
                    [{"group": "开发组", "text": "再补充", "mention": ["Lin"], "reply_to": ANSWER}]))
        lin = iter(([{"group": "开发组", "text": "收到", "mention": ["Ada"], "reply_to": ANSWER}],))
        self.speeches = {"Ada": lambda: next(ada, []), "Lin": lambda: next(lin, [])}
        with patch("redpanda.assistant.host.agent_groups.MAX_GROUP_REPLIES", 2):
            await self.host.agent_groups.post(group.group_id, "Ada 先说", ["Ada"])
            await self.settled(group)
        self.assertEqual([[result["code"] for result in work.result()] for work in self.work],
                         [["DELIVERED"], ["DELIVERED"], ["GROUP_LIMIT_REACHED"]])
        self.assertEqual(self.calls, ["Ada", "Lin", "Ada"])
        self.assertEqual(len(await self.host.agent_groups.messages(group.group_id)), 3)

    async def test_a_later_speech_calls_whom_it_names_after_its_discussion_ran_out(self):
        # A tester acknowledges the call, keeps working, then reports to the coordinator.
        group = await self.group()
        lin = iter(([{"group": "开发组", "text": "开工", "reply_to": ANSWER},
                     {"group": "开发组", "text": "报告：通过", "mention": ["Ada"]}],))
        self.speeches = {"Lin": lambda: next(lin, [])}
        with patch("redpanda.assistant.host.agent_groups.MAX_GROUP_ROUNDS", 1):
            await self.host.agent_groups.post(group.group_id, "Lin 去跑测试", ["Lin"])
            await self.settled(group)
        acknowledged, reported = self.work[0].result()
        self.assertIn("轮数上限", acknowledged["data"]["notice"])
        self.assertIn("依次回应", reported["data"]["notice"])
        self.assertEqual(self.calls, ["Lin", "Ada"])

    async def say(self, name, text, *, origin, reply_to=None, mention=()):
        """A speech straight from a member's own work, bypassing how its source is derived."""
        async def admit(_group):
            return None
        return await self.host.agent_groups.send(name, {
            "group": "开发组", "text": text, "mention": list(mention), "reply_to": reply_to,
            "message_id": f"{name}-{text}"}, origin, admit)

    async def running(self, name):
        """The member decides on what it has pending and starts a command that runs until released."""
        finished = asyncio.Event()

        async def run(_context, _arguments):
            await finished.wait()
            return {"ok": True}

        runtime = AgentRuntime(self.journal(name), ScriptedDecisionMaker(
            (lambda _: ModelDecision(command_requests=(InvokeTool("run_command"),)),)),
            {"run_command": ToolBinding(run)})
        self.assertIsNotNone((await runtime.advance(name)).step)
        await runtime.dispatcher.start_pending(name)

        async def finish():
            finished.set()
            while runtime.dispatcher.active_count:
                await asyncio.sleep(0)

        self.addAsyncCleanup(finish)
        return finish

    async def test_a_busy_member_blocks_no_one_and_its_unquoting_report_calls_whom_it_names(self):
        group = await self.group()
        task = await self.host.agent_groups.post(group.group_id, "Lin 去跑 #A", ["Lin"])
        await self.settled(group)
        # Lin takes up #A and is still running it when Third's own question calls Lin and then Ada.
        await self.running("Lin")
        self.calls.clear()
        await self.say("Third", "谁有空", origin="routine", mention=["Lin", "Ada"])
        await self.settled(group)
        self.assertEqual(self.calls, ["Ada"])
        self.assertEqual(len(await self.facts("Lin", GROUP_ATTENTION)), 2)
        # Lin finishes #A and reports without quoting Third's call: a speech of its own.
        report = await self.say("Lin", "#A 报告", origin=task.message_id, mention=["Third"])
        await self.settled(group)
        self.assertIn("依次回应", report["data"]["notice"])
        self.assertEqual(self.calls, ["Ada", "Third"])

    async def test_an_idle_member_is_waited_for_even_while_its_worker_reports_running(self):
        # A Worker started just to take in a group message reports running for a moment.
        group = await self.group()
        release = asyncio.Event()

        async def slow(session_id):
            self.calls.append(session_id)
            if session_id == "Ada":
                await release.wait()

        self.host.wait_quiescent.side_effect = slow
        with patch.object(self.host, "activity", return_value="running"):
            await self.host.agent_groups.post(group.group_id, "大家看看", [])
            async with asyncio.timeout(5):
                while not self.host.agent_groups._drives[group.group_id].waiting:
                    await asyncio.sleep(.01)
            self.assertEqual(self.host.agent_groups._drives[group.group_id].waiting.keys(), {"Ada"})
            release.set()
            await self.settled(group)
        self.assertEqual(self.calls, list(MEMBERS))

    async def test_a_persons_new_message_replaces_the_discussion_in_progress(self):
        group = await self.group()
        release = asyncio.Event()

        async def slow(session_id):
            self.calls.append(session_id)
            if session_id == "Ada":
                await release.wait()

        self.host.wait_quiescent.side_effect = slow
        old = await self.host.agent_groups.post(group.group_id, "大家看看", [])
        while self.calls != ["Ada"]:
            await asyncio.sleep(0.01)
        await self.host.agent_groups.post(group.group_id, "先别管了，Third 看这个", ["Third"])
        release.set()
        await self.settled(group)
        # Lin was never called for the replaced message; the new one calls only Third.
        self.assertEqual(self.calls, ["Ada", "Third"])
        lin_called_for = [parse_agent_fact(fact).message_id for fact in await self.facts("Lin", GROUP_ATTENTION)]
        self.assertNotIn(old.message_id, lin_called_for)

    async def test_a_speech_answers_a_call_only_by_quoting_it_and_only_once(self):
        group = await self.group()
        with patch("redpanda.assistant.host.agent_groups.MAX_GROUP_ROUNDS", 1):
            asked = await self.host.agent_groups.post(group.group_id, "Lin 看看", ["Lin"])
            await self.settled(group)
            other = await self.say("Third", "顺便一提", origin="routine", mention=["Ada"])
            await self.settled(group)
            self.calls.clear()
            # Quoting a message Lin was not called to answer is a plain quote.
            quoted = await self.say("Lin", "引用", origin=asked.message_id, reply_to=other["data"]["message_id"],
                                    mention=["Ada"])
            answered = await self.say("Lin", "回应", origin=asked.message_id, reply_to=asked.message_id,
                                      mention=["Ada"])
            again = await self.say("Lin", "再回应", origin=asked.message_id, reply_to=asked.message_id,
                                   mention=["Third"])
            await self.settled(group)
        self.assertIn("依次回应", quoted["data"]["notice"])
        self.assertIn("轮数上限", answered["data"]["notice"])
        self.assertIn("依次回应", again["data"]["notice"])
        self.assertEqual(self.calls, ["Ada", "Third"])

    async def test_another_speech_joins_the_queue_without_dropping_it(self):
        group = await self.group()
        release = asyncio.Event()

        async def slow(session_id):
            self.calls.append(session_id)
            if session_id == "Ada":
                await release.wait()

        self.host.wait_quiescent.side_effect = slow
        await self.host.agent_groups.post(group.group_id, "大家看看", [])
        while self.calls != ["Ada"]:
            await asyncio.sleep(0.01)
        await self.say("Third", "顺便说一句", origin="routine", mention=["Ada"])
        release.set()
        await self.settled(group)
        self.assertEqual(self.calls, ["Ada", "Lin", "Third", "Ada"])

    async def test_observers_hear_when_the_drive_ends(self):
        group = await self.group(("Ada", "Lin"))
        seen = []

        async def changed(group_id):
            view = await self.host.agent_groups.view(group_id)
            seen.append((view["calling"], len(view["messages"])))

        self.host.group_changed_sink = changed
        await self.host.agent_groups.post(group.group_id, "大家好", [])
        await self.settled(group)
        await asyncio.gather(*self.host.watchers)
        # The last notice comes after the drive ended, so a page reading it is never left "calling".
        self.assertEqual(seen[-1], ([], 1))

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

    async def test_group_receipt_replays_after_speaker_and_mentioned_member_rename(self):
        group = await self.group(("Ada", "Lin"))
        first = await self.say("Ada", "请核对", origin="human", mention=["Lin"])
        await self.settled(group)
        await self.host.rename_agent("Lin", "小林")
        await self.host.rename_agent("Ada", "艾达")
        again = await self.say("Ada", "请核对", origin="human", mention=["Lin"])
        self.assertEqual(first, again)
        self.assertEqual(len(await self.host.agent_groups.messages(group.group_id)), 1)
        self.assertEqual((await self.say("Ada", "新发言", origin="human", mention=["Lin"]))["code"],
                         "INVALID_MENTION")
