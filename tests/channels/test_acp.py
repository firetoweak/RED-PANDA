from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from acp import PROTOCOL_VERSION, RequestError

from redpanda.channels.acp import RedPandaAcpAgent
from redpanda.sandbox.registry import WorkspaceRegistry


class _Sessions:
    def __init__(self) -> None:
        self.calls = []
        self.accepted = asyncio.Event()
        self.quiescent = asyncio.Event()

    async def create(self, session_id, workspace_id):
        self.calls.append(("create", session_id, workspace_id))

    async def select(self, owner, session_id):
        self.calls.append(("select", owner, session_id))
        return SimpleNamespace()

    async def accept_input(self, session_id, content, **kwargs):
        self.calls.append(("accept_input", session_id, content, kwargs))
        self.accepted.set()
        return SimpleNamespace(control_approval=None, control_message=None)

    async def wait_quiescent(self, session_id):
        await self.quiescent.wait()
        return SimpleNamespace()

    async def cancel_turn(self, session_id):
        self.calls.append(("cancel_turn", session_id))
        return SimpleNamespace()

    async def release(self, owner):
        self.calls.append(("release", owner))


class _Client:
    def __init__(self) -> None:
        self.updates = []

    async def session_update(self, **kwargs):
        self.updates.append(kwargs)


class AcpChannelTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.sessions = _Sessions()
        self.client = _Client()
        self.workspaces = WorkspaceRegistry.load(self.root / "workspaces.json")
        self.agent = RedPandaAcpAgent(self.sessions, self.workspaces)
        self.agent.on_connect(self.client)
        await self.agent.initialize(PROTOCOL_VERSION)

    async def asyncTearDown(self) -> None:
        await self.agent.close()
        self.directory.cleanup()

    async def _new_session(self) -> str:
        response = await self.agent.new_session(str(self.root), mcp_servers=[])
        return response.session_id

    async def test_prompt_streams_output_until_session_is_quiescent(self) -> None:
        from acp.schema import TextContentBlock

        session_id = await self._new_session()
        prompt = asyncio.create_task(
            self.agent.prompt(
                session_id,
                [TextContentBlock(type="text", text="hello")],
            )
        )
        await self.sessions.accepted.wait()

        await self.agent.deliver(session_id, "output-1", "world")
        self.sessions.quiescent.set()
        response = await prompt

        self.assertEqual(response.stop_reason, "end_turn")
        self.assertEqual(
            self.client.updates[0]["update"].content.text,
            "world",
        )
        accepted = next(call for call in self.sessions.calls if call[0] == "accept_input")
        self.assertEqual(accepted[2], "hello")
        self.assertEqual(accepted[3]["source"], "acp")

    async def test_each_delivery_is_its_own_agent_message(self) -> None:
        from acp.schema import TextContentBlock

        session_id = await self._new_session()
        prompt = asyncio.create_task(
            self.agent.prompt(
                session_id,
                [TextContentBlock(type="text", text="hello")],
            )
        )
        await self.sessions.accepted.wait()

        await self.agent.deliver(session_id, "output-1", "first")
        await self.agent.deliver(session_id, "output-2", "second")
        self.sessions.quiescent.set()
        await prompt

        first, second = (item["update"] for item in self.client.updates[:2])
        self.assertEqual(first.content.text, "first")
        self.assertEqual(second.content.text, "second")
        self.assertNotEqual(first.message_id, second.message_id)

    async def test_preview_chunks_share_message_and_final_is_not_duplicated(self) -> None:
        from acp.schema import TextContentBlock

        session_id = await self._new_session()
        prompt = asyncio.create_task(
            self.agent.prompt(
                session_id,
                [TextContentBlock(type="text", text="hello")],
            )
        )
        await self.sessions.accepted.wait()

        await self.agent.preview(session_id, "started", "output-1", None)
        await self.agent.preview(session_id, "delta", "output-1", "wor")
        await self.agent.preview(session_id, "delta", "output-1", "ld")
        await self.agent.deliver(session_id, "output-1", "world")
        self.sessions.quiescent.set()
        await prompt

        chunks = [item["update"] for item in self.client.updates]
        self.assertEqual([chunk.content.text for chunk in chunks], ["wor", "ld"])
        self.assertEqual(
            {chunk.message_id for chunk in chunks},
            {"message-output-1"},
        )

        await self.agent.deliver(session_id, "output-1", "world")
        self.assertEqual(len(self.client.updates), 2)

    async def test_cancel_stops_decision_before_returning_cancelled(self) -> None:
        from acp.schema import TextContentBlock

        session_id = await self._new_session()
        prompt = asyncio.create_task(
            self.agent.prompt(
                session_id,
                [TextContentBlock(type="text", text="wait")],
            )
        )
        await self.sessions.accepted.wait()

        await self.agent.cancel(session_id)
        response = await prompt

        self.assertEqual(response.stop_reason, "cancelled")
        self.assertIn(("cancel_turn", session_id), self.sessions.calls)

        await self.agent.deliver(session_id, "output-1", "late output")
        self.assertEqual(self.client.updates, [])

    async def test_new_session_registers_unregistered_cwd(self) -> None:
        response = await self.agent.new_session(str(self.root), mcp_servers=[])

        created = next(
            call for call in self.sessions.calls if call[0] == "create"
        )
        workspace = self.workspaces.find_by_path(self.root)
        self.assertIsNotNone(workspace)
        self.assertEqual(created[2], workspace.workspace_id)
        self.assertEqual(response.session_id, created[1])

    async def test_new_session_reuses_the_deepest_workspace(self) -> None:
        nested = self.root / "nested"
        nested.mkdir()
        inner = self.workspaces.create(name="nested", task_root=nested)

        await self.agent.new_session(str(nested), mcp_servers=[])

        created = next(
            call for call in self.sessions.calls if call[0] == "create"
        )
        self.assertEqual(created[2], inner.workspace_id)

    async def test_new_session_rejects_invalid_cwd(self) -> None:
        with self.assertRaises(RequestError) as raised:
            await self.agent.new_session("relative", mcp_servers=[])

        self.assertEqual(raised.exception.code, -32602)
        self.assertEqual(self.sessions.calls, [])

    async def test_tool_progress_is_projected_as_session_updates(self) -> None:
        session_id = await self._new_session()

        await self.agent.report_tool(
            session_id, "start", "command-1", "read_file", {"path": "a.txt"}
        )
        await self.agent.report_tool(
            session_id,
            "finish",
            "command-1",
            "read_file",
            {"ok": False, "code": "NOT_FOUND", "error": "missing"},
        )

        start, finish = (item["update"] for item in self.client.updates[-2:])
        self.assertEqual(start.session_update, "tool_call")
        self.assertEqual(start.kind, "read")
        self.assertEqual(start.status, "in_progress")
        self.assertEqual(finish.session_update, "tool_call_update")
        self.assertEqual(finish.status, "failed")
        self.assertEqual(finish.content[0].content.text, "missing")


if __name__ == "__main__":
    unittest.main()
