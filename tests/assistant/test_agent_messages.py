from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from redpanda.assistant.agent_messages import (
    LIST_COLLEAGUES, SEND_MESSAGE, message_bindings, recover_message_attempts, send_message,
)
from redpanda.assistant.agents import (
    COMMUNICATION_LIMIT_REACHED, PRIVATE_MESSAGE, AgentCreated, fact_arguments, parse_agent_fact,
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
            await self.store.create(session_id, workspace_id=workspace.workspace_id, initial_fact=fact_arguments(
                AgentCreated(name=session_id, workspace_id=workspace.workspace_id,
                             worktree=str(project), branch=f"agent/{session_id}"), delivery_id="created"))

        async def deliver(operation, session_id, arguments):
            self.assertEqual(operation, "fact")
            await AgentRuntime(self.journal(session_id), None, {}).receive_domain_fact(session_id, **arguments)

        self.deliver = AsyncMock(side_effect=deliver)
        self.request_patch = patch.object(self.host, "request", self.deliver)
        self.request_patch.start()

    async def asyncTearDown(self):
        self.request_patch.stop()
        await self.host.close()
        self.directory.cleanup()

    def journal(self, session_id):
        return SqliteJournal(self.store.require(session_id))

    async def transport(self, operation, session_id, arguments):
        return await self.host._route(operation, session_id, arguments)

    async def send(self, sender="Ada", recipient="Lin", command="one", reply_to=None):
        return await send_message(self.transport, sender, command,
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
            await send_message(self.transport, "Ada", "one", {"recipient": "Lin", "text": "different"})

    async def test_known_address_errors_and_same_pair_reply_validation(self):
        for name, code in (("Nobody", "RECIPIENT_NOT_FOUND"), ("Ada", "SELF_MESSAGE"),
                           ("Elsewhere", "DIFFERENT_PROJECT")):
            self.assertEqual((await self.send(recipient=name))["code"], code)
        foreign = await self.send("Third", "Lin", "foreign")
        for reference in ("missing", foreign["data"]["message_id"]):
            self.assertEqual((await self.send(reply_to=reference))["code"], "INVALID_REPLY")
        self.assertEqual(len(await self.messages("Lin")), 1)
        colleagues = await self.transport(LIST_COLLEAGUES, "Ada", {})
        self.assertEqual(colleagues, {"ok": True, "code": "COLLEAGUES_READ", "data": {"colleagues": ["Lin", "Third"]}})

    async def test_external_arguments_cannot_supply_sender_thread_or_count(self):
        for extra in ({"sender": "Third"}, {"hop": 0}, {"thread_id": "new"}, {"text": " "}):
            result = await send_message(self.transport, "Ada", "one", {"recipient": "Lin", "text": "hi", **extra})
            self.assertEqual(result["code"], "INVALID_ARGUMENT")
        self.assertEqual(await self.messages("Lin"), [])

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
        runtime = AgentRuntime(self.journal("Lin"), ScriptedDecisionMaker((lambda _: ModelDecision(
            command_requests=(InvokeTool(SEND_MESSAGE, tuple(args.items())),)),)), {SEND_MESSAGE: binding})
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
            await self.store.create(session_id, workspace_id=self.workspace.workspace_id, initial_fact=fact_arguments(
                AgentCreated(name=session_id, workspace_id=self.workspace.workspace_id,
                             worktree=str(self.root), branch="sender"), delivery_id="created"))
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
