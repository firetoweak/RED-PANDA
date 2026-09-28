from __future__ import annotations

import unittest

from helperme.assistant.compact.store import ConversationStatus
from helperme.channels.web.hub import WebEventHub


class WebEventHubTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub = WebEventHub()

    async def test_broadcasts_to_every_connection_and_survives_empty_subscribers(self):
        first = self.hub.subscribe()
        second = self.hub.subscribe()

        await self.hub.session_activity("session-a", "running")
        await self.hub.output_final("session-b", "out-1", "done")

        self.assertEqual((await first.get()).data["session_id"], "session-a")
        self.assertEqual((await first.get()).name, "output_final")
        self.assertEqual((await second.get()).name, "session_activity")
        self.hub.unsubscribe(first)
        self.hub.unsubscribe(second)
        await self.hub.output_final("session-b", "out-2", "still-ok")

    async def test_keeps_one_preview_per_session_and_clears_it_on_final(self):
        queue = self.hub.subscribe()

        await self.hub.preview("session-a", "started", "out-1", None)
        await self.hub.preview("session-a", "delta", "out-1", "你")
        await self.hub.preview("session-a", "delta", "out-1", "好")
        with self.assertRaises(RuntimeError):
            await self.hub.preview("session-a", "delta", "out-other", "x")
        await self.hub.output_final("session-a", "out-1", "你好")

        names = [(await queue.get()).name for _ in range(4)]
        self.assertEqual(
            names,
            ["preview.started", "preview.delta", "preview.delta", "output_final"],
        )
        await self.hub.preview("session-a", "started", "out-2", None)
        await self.hub.preview("session-a", "delta", "out-2", "x")
        self.hub.unsubscribe(queue)

    async def test_final_of_a_superseded_output_still_lands(self):
        queue = self.hub.subscribe()

        await self.hub.preview("session-a", "started", "out-1", None)
        await self.hub.preview("session-a", "started", "out-2", None)
        await self.hub.output_final("session-a", "out-1", "旧的")
        await self.hub.preview("session-a", "delta", "out-2", "新的")

        names = [(await queue.get()).name for _ in range(4)]
        self.assertEqual(
            names,
            [
                "preview.started",
                "preview.started",
                "output_final",
                "preview.delta",
            ],
        )
        self.hub.unsubscribe(queue)

    async def test_tool_progress_merges_by_command_identity_without_payload(self):
        queue = self.hub.subscribe()

        await self.hub.tool_progress(
            "session-a", "start", "cmd-1", "read_file", {"path": "a"}
        )
        await self.hub.tool_progress("session-a", "finish", "cmd-1", "read_file", "ok")
        with self.assertRaises(ValueError):
            await self.hub.tool_progress("session-a", "unknown", "cmd-1", "read_file", None)

        started = await queue.get()
        finished = await queue.get()
        self.assertEqual(started.name, "tool_progress")
        self.assertEqual(
            started.data,
            {
                "session_id": "session-a",
                "command_id": "cmd-1",
                "name": "read_file",
                "status": "running",
            },
        )
        self.assertEqual(finished.data["status"], "settled")
        self.hub.unsubscribe(queue)

    async def test_context_usage_is_session_scoped(self):
        queue = self.hub.subscribe()

        await self.hub.context_usage("session-a", 1200, 200000)
        event = await queue.get()

        self.assertEqual(event.name, "context_usage")
        self.assertEqual(
            event.data,
            {"session_id": "session-a", "used": 1200, "compact_threshold_tokens": 200000},
        )
        self.hub.unsubscribe(queue)

    async def test_thinking_is_a_separate_stream_from_preview(self):
        queue = self.hub.subscribe()

        await self.hub.thinking("session-a", "started", "out-1", None)
        await self.hub.thinking("session-a", "delta", "out-1", "想")
        await self.hub.preview("session-a", "started", "out-1", None)
        await self.hub.preview("session-a", "delta", "out-1", "答")
        await self.hub.thinking("session-a", "finished", "out-1", None)

        names = [(await queue.get()).name for _ in range(5)]
        self.assertEqual(
            names,
            [
                "thinking.started",
                "thinking.delta",
                "preview.started",
                "preview.delta",
                "thinking.finished",
            ],
        )
        self.hub.unsubscribe(queue)

    async def test_conversation_status_is_session_scoped(self):
        queue = self.hub.subscribe()

        await self.hub.conversation_status(
            ConversationStatus("session-a", "session-a", 1, "running")
        )
        event = await queue.get()

        self.assertEqual(event.name, "conversation_status")
        self.assertEqual(
            event.data,
            {
                "session_id": "session-a",
                "compact_count": 1,
                "compact_phase": "running",
            },
        )
        self.hub.unsubscribe(queue)

    async def test_session_failed_is_session_scoped(self):
        queue = self.hub.subscribe()

        await self.hub.session_failed("session-a", "运行失败：模型服务暂时不可用")
        event = await queue.get()

        self.assertEqual(event.name, "session_failed")
        self.assertEqual(
            event.data,
            {
                "session_id": "session-a",
                "message": "运行失败：模型服务暂时不可用",
            },
        )
        self.hub.unsubscribe(queue)

    async def test_authorization_required_carries_command_arguments(self):
        queue = self.hub.subscribe()

        await self.hub.authorization_required(
            "session-a",
            "cmd-1",
            "write_file",
            {"path": "a.md"},
        )
        event = await queue.get()

        self.assertEqual(event.name, "authorization_required")
        self.assertEqual(
            event.data,
            {
                "session_id": "session-a",
                "command_id": "cmd-1",
                "name": "write_file",
                "arguments": {"path": "a.md"},
            },
        )
        self.hub.unsubscribe(queue)
