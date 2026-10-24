import asyncio
import multiprocessing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from konden.assistant.authorization import (
    AuthorizationConflict,
    AuthorizationTargetNotFound,
)
from konden.assistant.host.ipc import PipePeer
from konden.assistant.inputs import InputConflict
from konden.assistant.sessions import AssistantSessions
from konden.runtime import InvokeTool, ModelDecision, SqliteJournal
from tests.fixtures.memory_journal import MemoryJournal
from tests.assistant.test_runner import ScriptedDecisionMaker
from tests.runtime.test_boundary_slice import RecordingTool, runtime_for


class AuthorizationBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_and_processed_targets_do_not_add_events_or_wake(self):
        for persistent in (False, True):
            for approved in (False, True):
                with self.subTest(persistent=persistent, approved=approved), TemporaryDirectory() as root:
                    journal = SqliteJournal(Path(root) / "journal.sqlite") if persistent else MemoryJournal()
                    tool = RecordingTool("write", requires_authorization=True)
                    runtime = runtime_for(tool, ScriptedDecisionMaker((
                        lambda _: ModelDecision(command_requests=(InvokeTool("write"),)),
                    )), journal)
                    scheduler = AsyncMock()
                    sessions = AssistantSessions(
                        runtime, None, scheduler, control=None, management=None, catalog=None,
                    )
                    await runtime.receive_user_message("session", "write", delivery_id="input")
                    step = await runtime.advance("session")
                    command_id = step.step.commands[0].command_id
                    try:
                        before = await runtime.snapshot("session")
                        with self.assertRaises(AuthorizationTargetNotFound):
                            await sessions.resolve_authorization("session", "missing", approved=approved)
                        self.assertEqual(await runtime.snapshot("session"), before)
                        scheduler.wake.assert_not_awaited()

                        await sessions.resolve_authorization("session", command_id, approved=approved)
                        scheduler.wake.assert_awaited_once_with("session")
                        scheduler.wake.reset_mock()
                        if approved:
                            await tool.started.wait()
                        before = await runtime.snapshot("session")
                        for repeated_approval in (False, True):
                            with self.assertRaises(AuthorizationConflict):
                                await sessions.resolve_authorization(
                                    "session", command_id, approved=repeated_approval,
                                )
                            with self.assertRaises(AuthorizationConflict):
                                await sessions.resolve_authorizations(
                                    "session", approved=repeated_approval,
                                )
                        self.assertEqual(await runtime.snapshot("session"), before)
                        scheduler.wake.assert_not_awaited()
                        self.assertEqual(len(tool.executions), int(approved))
                    finally:
                        tool.release.set()
                        while runtime.dispatcher.active_count:
                            await asyncio.sleep(0)

    async def test_internal_failures_are_not_authorization_conflicts(self):
        runtime = runtime_for(RecordingTool("write", requires_authorization=True), ScriptedDecisionMaker((
            lambda _: ModelDecision(command_requests=(InvokeTool("write"),)),
        )))
        await runtime.receive_user_message("session", "write", delivery_id="input")
        step = await runtime.advance("session")
        sessions = AssistantSessions(
            runtime, None, AsyncMock(), control=None, management=None, catalog=None,
        )
        for method, approved in (("grant_command", True), ("reject_command", False)):
            for error in (ValueError("invalid journal"), KeyError("missing stored command")):
                with patch.object(runtime, method, AsyncMock(side_effect=error)):
                    with self.assertRaises(type(error)) as caught:
                        await sessions.resolve_authorization(
                            "session", step.step.commands[0].command_id, approved=approved,
                        )
                    self.assertIs(caught.exception, error)


class AuthorizationPipeTest(unittest.IsolatedAsyncioTestCase):
    async def test_known_authorization_errors_round_trip_without_failing_worker(self):
        left, right = multiprocessing.Pipe()

        async def handle(operation, session_id, arguments):
            if arguments["missing"]:
                raise AuthorizationTargetNotFound("target missing")
            raise AuthorizationConflict("already processed")

        async def unused(*args):
            raise AssertionError(args)

        host = PipePeer(left, unused, unused)
        worker = PipePeer(right, handle, unused)
        readers = [asyncio.create_task(peer.run()) for peer in (host, worker)]
        try:
            for operation in ("resolve_authorization", "resolve_authorizations"):
                for missing, expected in ((True, AuthorizationTargetNotFound), (False, AuthorizationConflict)):
                    with self.assertRaises(expected):
                        await asyncio.wait_for(host.request(operation, "session", {"missing": missing}), 5)
                    self.assertIsNone(worker.failure)
                    self.assertFalse(readers[1].done())
        finally:
            await host.close(RuntimeError("done"))
            await worker.close(RuntimeError("done"))
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            left.close()
            right.close()

    async def test_unknown_failure_and_wrong_operation_are_not_error_responses(self):
        for operation, error in (
            ("resolve_authorization", ValueError("invalid journal")),
            ("view", AuthorizationConflict("wrong boundary")),
            ("accept_input", ValueError("invalid journal")),
            ("view", InputConflict("wrong boundary")),
            ("resolve_authorization", InputConflict("wrong boundary")),
            ("accept_input", AuthorizationConflict("wrong boundary")),
        ):
            peer = PipePeer(None, AsyncMock(side_effect=error), None)
            peer.send = AsyncMock()
            with self.assertRaises(type(error)) as caught:
                await peer._handle("request", operation, "session", {})
            self.assertIs(caught.exception, error)
            peer.send.assert_not_awaited()
