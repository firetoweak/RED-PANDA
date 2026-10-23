from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from redpanda.assistant.agent_messages import (
    DECLARE_DIRECTION, LIST_COLLEAGUES, SEND_MESSAGE, message_bindings, recover_message_attempts, send_message,
)
from redpanda.assistant.agents import (
    COMMUNICATION_LIMIT_REACHED, DIRECTION_DECLARED, DIRECTION_SET_BY_USER, PRIVATE_MESSAGE, AgentCreated, AgentRecord,
    agent_fact_for_model, fact_arguments, identity_for_model, parse_agent_fact,
)
from redpanda.assistant.conversations import project_conversation
from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.assistant.sessions import session_view
from redpanda.filesystem.registry import WorkspaceRegistry
from redpanda.paths import RedPandaHome
from redpanda.runtime import AgentRuntime, CommandPhase, DomainFactCommitted, InvokeTool, ModelDecision, SqliteJournal, ToolBinding, replay
from redpanda.runtime.dispatcher import AttemptContext
from tests.assistant.test_runner import ScriptedDecisionMaker


class AgentMessagesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.home = RedPandaHome(self.root / "home")
        self.store = SessionStore(self.home.runtime_sessions_root)
        project = self.root / "project"
        project.mkdir()
        registry = WorkspaceRegistry.load(self.home.workspaces_path)
        self.workspace = registry.register_path(project)
        other = self.root / "other"
        other.mkdir()
        self.other = registry.register_path(other)
        self.host = HostSupervisor(self.store, None, self.home, None, llm=None, workspaces=registry)
        for session_id, workspace in (("Ada", self.workspace), ("Lin", self.workspace),
                                      ("Third", self.workspace), ("Elsewhere", self.other)):
            await self.store.create(session_id, directory=project, initial_fact=fact_arguments(
                AgentCreated(name=session_id, directory=str(project)), delivery_id="created"))

        async def deliver(operation, session_id, arguments):
            self.assertEqual(operation, "fact")
            await AgentRuntime(self.journal(session_id), None, {}).receive_domain_fact(session_id, **arguments)

        self.deliver = AsyncMock(side_effect=deliver)
        self.request_patch = patch.object(self.host, "request", self.deliver)
        self.request_patch.start()
        self.commands = {}

    async def asyncTearDown(self):
        self.request_patch.stop()
        await self.host.close()
        self.directory.cleanup()

    def journal(self, session_id):
        return SqliteJournal(self.store.require(session_id))

    async def transport(self, operation, session_id, arguments):
        return await self.host._route(operation, session_id, arguments)

    async def command(self, sender, label):
        """A real Command in the sender's Journal; the same label reuses it."""
        key = (sender, label)
        if key not in self.commands:
            runtime = AgentRuntime(self.journal(sender), ScriptedDecisionMaker((lambda _: ModelDecision(
                command_requests=(InvokeTool(SEND_MESSAGE, (("recipient", "x"), ("text", "t"))),)),)),
                {SEND_MESSAGE: ToolBinding(AsyncMock(return_value={
                    "ok": False, "code": "RECIPIENT_NOT_FOUND", "error": "没有这位同事。",
                }))})
            await runtime.receive_user_message(sender, label, delivery_id=label)
            self.commands[key] = (await runtime.advance(sender)).step.commands[0].command_id
            # Settle it so the sender's next decision is not held behind it.
            await runtime.dispatcher.start_pending(sender)
            while runtime.dispatcher.active_count:
                await asyncio.sleep(0)
        return self.commands[key]

    async def send(self, sender="Ada", recipient="Lin", command="one", reply_to=None):
        return await send_message(self.transport, sender, await self.command(sender, command),
                                  {"recipient": recipient, "text": "请核对接口", "reply_to": reply_to})

    async def messages(self, session_id):
        return [parse_agent_fact(event.payload) for event in await self.journal(session_id).snapshot(session_id)
                if isinstance(event.payload, DomainFactCommitted) and event.payload.fact_type == PRIVATE_MESSAGE]

    async def test_delivery_identity_is_stable_and_reply_inherits_thread(self):
        first = await self.send()
        again = await self.send()
        self.assertEqual(first, again)
        self.deliver.assert_awaited_once()
        reply = await self.send("Lin", "Ada", "reply", first["data"]["message_id"])
        received, = await self.messages("Lin")
        response, = await self.messages("Ada")
        self.assertEqual(received.hop, 0)
        self.assertEqual(response.hop, 1)
        self.assertEqual(response.thread_id, received.thread_id)
        self.assertEqual(response.message_id, reply["data"]["message_id"])
        self.assertEqual(response.sender.name, "Lin")
        with self.assertRaisesRegex(ValueError, "conflicts"):
            await send_message(self.transport, "Ada", await self.command("Ada", "one"),
                               {"recipient": "Lin", "text": "different"})

    async def test_renamed_peer_keeps_replies_and_delivery_receipts(self):
        first = await self.send()
        await self.host.rename_agent("Lin", "小林")
        retried = await self.send()
        self.assertEqual(retried["data"]["message_id"], first["data"]["message_id"])
        self.assertEqual(retried["data"]["recipient"], "小林")
        self.assertEqual(len(await self.messages("Lin")), 1)
        self.assertEqual((await self.send(recipient="Lin", command="old-address"))["code"], "RECIPIENT_NOT_FOUND")
        reply = await self.send(sender="Lin", recipient="Ada", command="reply", reply_to=first["data"]["message_id"])
        self.assertEqual(reply["code"], "DELIVERED")
        response, = await self.messages("Ada")
        self.assertEqual(response.sender.name, "小林")
        self.assertEqual(response.hop, 1)

    async def test_known_address_errors_and_same_pair_reply_validation(self):
        for name, code in (("Nobody", "RECIPIENT_NOT_FOUND"), ("Ada", "SELF_MESSAGE")):
            self.assertEqual((await self.send(recipient=name))["code"], code)
        foreign = await self.send("Third", "Lin", "foreign")
        for reference in ("missing", foreign["data"]["message_id"]):
            self.assertEqual((await self.send(reply_to=reference))["code"], "INVALID_REPLY")
        self.assertEqual(len(await self.messages("Lin")), 1)
        colleagues = await self.transport(LIST_COLLEAGUES, "Ada", {})
        self.assertEqual([{key: entry[key] for key in ("name", "direction")}
                          for entry in colleagues["data"]["colleagues"]],
                         [{"name": "Elsewhere", "direction": None}, {"name": "Lin", "direction": None},
                          {"name": "Third", "direction": None}])
        self.assertEqual({entry["directory"] for entry in colleagues["data"]["colleagues"]},
                         {str((await self.host.agents.resolve(name)).directory)
                          for name in ("Elsewhere", "Lin", "Third")})

    async def test_colleague_status_follows_host_facts_without_internal_identifiers(self):
        await self.host.set_auto_authorize(False)
        runtime = AgentRuntime(self.journal("Lin"), ScriptedDecisionMaker((lambda _: ModelDecision(
            command_requests=(InvokeTool("write_file", (("path", "a.txt"),)),)),)),
            {"write_file": ToolBinding(AsyncMock(), requires_authorization=True)})
        await runtime.advance("Lin")

        async def statuses():
            colleagues = (await self.transport(LIST_COLLEAGUES, "Ada", {}))["data"]["colleagues"]
            for entry in colleagues:
                self.assertEqual(set(entry), {"name", "direction", "directory", "projects", "status", "since"})
                last = (await self.journal(entry["name"]).snapshot(entry["name"]))[-1]
                self.assertEqual(entry["since"], last.occurred_at.astimezone().isoformat(timespec="minutes"))
            return {entry["name"]: entry["status"] for entry in colleagues}

        self.assertEqual(await statuses(), {"Elsewhere": "idle", "Lin": "awaiting_authorization", "Third": "idle"})
        self.assertEqual(len((await self.host._project_view("Lin")).pending_authorization_ids), 1)
        await self.host.set_auto_authorize(True)
        self.assertEqual(await statuses(), {"Elsewhere": "idle", "Lin": "idle", "Third": "idle"})
        self.assertEqual((await self.host._project_view("Lin")).pending_authorization_ids, ())
        with patch.object(self.host, "activity", lambda session_id: "running" if session_id == "Lin" else "idle"):
            self.assertEqual(await statuses(), {"Elsewhere": "idle", "Lin": "working", "Third": "idle"})

    async def test_declared_direction_is_own_roster_entry_and_reaches_colleagues(self):
        binding = message_bindings(self.transport, self.journal("Ada"))[DECLARE_DIRECTION]
        for extra in ({"name": "Lin"}, {"direction": " "}):
            result = await binding.handler(AttemptContext("Ada", "bad", "attempt-bad", 1), {"direction": "x", **extra})
            self.assertEqual(result["code"], "INVALID_ARGUMENT")
        for command in ("first", "first", "second"):
            result = await binding.handler(AttemptContext("Ada", command, f"attempt-{command}", 1),
                                           {"direction": f"负责{command}"})
            self.assertEqual(result["code"], "DIRECTION_DECLARED")
        directions = {entry.agent.name: entry.direction for entry in await self.host.agents.list()}
        self.assertEqual(directions, {"Ada": "负责second", "Lin": None, "Third": None, "Elsewhere": None})
        colleagues = await self.transport(LIST_COLLEAGUES, "Lin", {})
        self.assertIn({"name": "Ada", "direction": "负责second"},
                      [{key: entry[key] for key in ("name", "direction")} for entry in colleagues["data"]["colleagues"]])
        for session_id, count in (("Ada", 2), ("Lin", 2), ("Third", 2), ("Elsewhere", 2)):
            received = [event for event in await self.journal(session_id).snapshot(session_id)
                        if isinstance(event.payload, DomainFactCommitted)
                        and event.payload.fact_type == DIRECTION_DECLARED]
            self.assertEqual(len(received), count, session_id)

    async def test_external_arguments_cannot_supply_sender_thread_or_count(self):
        for extra in ({"sender": "Third"}, {"hop": 0}, {"thread_id": "new"}, {"text": " "}):
            result = await send_message(self.transport, "Ada", "one", {"recipient": "Lin", "text": "hi", **extra})
            self.assertEqual(result["code"], "INVALID_ARGUMENT")
        self.assertEqual(await self.messages("Lin"), [])

    async def test_person_and_bot_share_latest_direction_in_context_roster_and_restart(self):
        binding = message_bindings(self.transport, self.journal("Ada"))[DECLARE_DIRECTION]
        await binding.handler(AttemptContext("Ada", "first", "attempt-first", 1), {"direction": "负责前端"})
        await self.host.set_direction_by_user("Ada", "负责测试")
        events = await self.journal("Ada").snapshot("Ada")
        latest = [event for event in events if isinstance(event.payload, DomainFactCommitted)
                  and event.payload.fact_type == DIRECTION_SET_BY_USER][-1]
        self.assertFalse(latest.payload.requests_decision)
        self.assertEqual(agent_fact_for_model(latest.payload)["source"], "user")
        self.assertEqual(identity_for_model(events)["direction"], "负责测试")
        for session_id in ("Lin", "Third", "Elsewhere"):
            facts = [event.payload for event in await self.journal(session_id).snapshot(session_id)
                     if isinstance(event.payload, DomainFactCommitted) and event.payload.fact_type == DIRECTION_SET_BY_USER]
            self.assertEqual(facts[-1].data["direction"], "负责测试")
            self.assertEqual(agent_fact_for_model(facts[-1])["source"], "user")
        from redpanda.assistant.host.agent_roster import AgentRoster
        rebuilt = AgentRoster(self.store)
        self.assertEqual({entry.agent.name: entry.direction for entry in await rebuilt.list()}["Ada"], "负责测试")
        await binding.handler(AttemptContext("Ada", "second", "attempt-second", 1), {"direction": "负责测试与验收"})
        colleagues = await self.transport(LIST_COLLEAGUES, "Lin", {})
        self.assertEqual(next(item["direction"] for item in colleagues["data"]["colleagues"]
                              if item["name"] == "Ada"), "负责测试与验收")

    async def test_tenth_reply_is_allowed_next_is_reported_durably_to_human(self):
        first = await self.send()
        reference = first["data"]["message_id"]
        for hop in range(1, 11):
            sender, recipient = ("Lin", "Ada") if hop % 2 else ("Ada", "Lin")
            result = await self.send(sender, recipient, f"reply-{hop}", reference)
            self.assertTrue(result["ok"])
            reference = result["data"]["message_id"]
        binding = message_bindings(self.transport, self.journal("Lin"))[SEND_MESSAGE]
        args = {"recipient": "Ada", "text": "继续", "reply_to": reference}
        # The Command waits for authorization, so only this test runs the handler; a background
        # dispatch would race it and still be writing Lin's Journal when the test ends.
        runtime = AgentRuntime(self.journal("Lin"), ScriptedDecisionMaker((lambda _: ModelDecision(
            command_requests=(InvokeTool(SEND_MESSAGE, tuple(args.items())),)),)),
            {SEND_MESSAGE: ToolBinding(binding.handler, requires_authorization=True)})
        await runtime.receive_user_message("Lin", "继续", delivery_id="limit")
        advanced = await runtime.advance("Lin")
        context = AttemptContext("Lin", advanced.step.commands[0].command_id, "attempt-limit", 1)
        for _ in range(2):
            result = await binding.handler(context, args)
            self.assertEqual(result["code"], "HOP_LIMIT_REACHED")
            self.assertNotIn("另起", result["error"])
        self.assertEqual(len(await self.messages("Ada")), 5)
        events = await self.journal("Lin").snapshot("Lin")
        notices = [event for event in events if isinstance(event.payload, DomainFactCommitted)
                   and event.payload.fact_type == COMMUNICATION_LIMIT_REACHED]
        self.assertEqual(len(notices), 1)
        self.assertFalse(notices[0].payload.requests_decision)
        state = replay("Lin", events)
        view = project_conversation("Lin", events, state.state.steps, session=session_view(state.state))
        self.assertIn("需要你判断", view.items[-1].text)

    async def test_narrow_recovery_resolves_original_command_with_or_without_prior_delivery(self):
        for delivered in (True, False):
            session_id = f"Sender-{delivered}"
            identity = AgentCreated(name=session_id, directory=str(self.root))
            await self.store.create(session_id, directory=self.root,
                                    initial_fact=fact_arguments(identity, delivery_id="created"))
            await self.host.agents.add(AgentRecord(session_id, identity))
            entered = asyncio.Event()

            async def interrupted(context, arguments):
                if delivered:
                    await send_message(self.transport, context.session_id, context.command_id, arguments)
                entered.set()
                await asyncio.Event().wait()

            journal = self.journal(session_id)
            runtime = AgentRuntime(journal, ScriptedDecisionMaker((lambda _: ModelDecision(command_requests=(
                InvokeTool(SEND_MESSAGE, (("recipient", "Lin"), ("text", "recover me"))),)),)),
                {SEND_MESSAGE: ToolBinding(interrupted)})
            await runtime.advance(session_id)
            await runtime.dispatcher.start_pending(session_id)
            await asyncio.wait_for(entered.wait(), 3)
            await runtime.dispatcher.close()
            await journal.prepare_recovery(session_id)
            self.assertEqual(replay(session_id, await journal.snapshot(session_id)).state.commands[0].phase, CommandPhase.UNKNOWN)
            await recover_message_attempts(journal, session_id, self.transport)
            await recover_message_attempts(journal, session_id, self.transport)
            state = replay(session_id, await journal.snapshot(session_id)).state
            command, = state.commands
            self.assertIsNotNone(command.outcome)
            self.assertEqual(command.outcome.value["code"], "DELIVERED")
            self.assertIsNot(command.phase, CommandPhase.UNKNOWN)
            matching = [message for message in await self.messages("Lin") if message.sender.name == session_id]
            self.assertEqual(len(matching), 1)
            self.assertEqual(command.outcome.value["data"]["message_id"], matching[0].message_id)

    async def test_one_origin_bounds_new_chains_and_the_replies_it_drives(self):
        async def decide(session_id, *decisions):
            runtime = AgentRuntime(self.journal(session_id), ScriptedDecisionMaker(decisions),
                                   {SEND_MESSAGE: ToolBinding(AsyncMock())})
            for _ in decisions:
                step = (await runtime.advance(session_id)).step
            return step.commands

        tasks = lambda _: ModelDecision(command_requests=tuple(
            InvokeTool(SEND_MESSAGE, (("recipient", "Lin"), ("text", f"任务{index}"))) for index in range(4)))
        await AgentRuntime(self.journal("Ada"), None, {}).receive_user_message("Ada", "分头去做", delivery_id="human")
        commands = await decide("Ada", lambda _: ModelDecision(content="你好"), tasks)
        with patch("redpanda.assistant.host.agent_messages.MAX_MESSAGES_PER_ORIGIN", 3):
            codes = [(await send_message(self.transport, "Ada", command.command_id,
                                         {"recipient": recipient, "text": "新话题"}))["code"]
                     for command, recipient in zip(commands, ("Lin", "Third", "Lin", "Third"))]
            # New chains cannot escape the budget of the human message that drove them.
            self.assertEqual(codes, ["DELIVERED"] * 3 + ["ORIGIN_LIMIT_REACHED"])
            reply, = await decide("Lin", lambda _: ModelDecision(content="你好"), lambda _: ModelDecision(
                command_requests=(InvokeTool(SEND_MESSAGE, (("recipient", "Ada"), ("text", "收到"))),)))
            result = await send_message(self.transport, "Lin", reply.command_id, {"recipient": "Ada", "text": "收到"})
            self.assertEqual(result["code"], "ORIGIN_LIMIT_REACHED")
            await AgentRuntime(self.journal("Third"), None, {}).receive_user_message("Third", "问问 Ada", delivery_id="human")
            # The human input overtakes Ada's pending message and has its own budget.
            fresh, = await decide("Third", lambda _: ModelDecision(
                command_requests=(InvokeTool(SEND_MESSAGE, (("recipient", "Ada"), ("text", "在吗"))),)))
            result = await send_message(self.transport, "Third", fresh.command_id, {"recipient": "Ada", "text": "在吗"})
            self.assertEqual(result["code"], "DELIVERED")
