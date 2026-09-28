from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, call, patch

from PIL import Image
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout import HSplit, Window
from prompt_toolkit.output import DummyOutput
from helperme.assistant.attachments import AttachmentGateway
from helperme.assistant.compact.store import ConversationStatus

from helperme.channels.tui.images import ConsoleMessage, ImagePaste


def _console():
    from helperme.channels.tui.console import (
        _BottomAnchoredPromptSession,
        _ContextMeter,
        read_console_input,
    )

    return _BottomAnchoredPromptSession, _ContextMeter, read_console_input


class ConsoleInputTests(unittest.IsolatedAsyncioTestCase):
    def test_clipboard_image_becomes_a_token_and_session_ref(self):
        with TemporaryDirectory() as directory:
            paste = ImagePaste(AttachmentGateway(Path(directory)))
            paste.bind("session-a")
            inserted = []

            class _Event:
                current_buffer = type(
                    "Buffer", (), {"insert_text": staticmethod(inserted.append)}
                )()
                key_sequence = [type("Key", (), {"key": "c-v"})()]
                data = ""

            with patch(
                "helperme.channels.tui.images.ImageGrab.grabclipboard",
                return_value=Image.new("RGB", (8, 8)),
            ):
                paste.paste(_Event())

            self.assertEqual(inserted, ["[Image #1]"])
            submitted = paste.submit("[Image #1]")
            self.assertEqual(submitted.text, "[Image #1]")
            self.assertEqual(len(submitted.artifact_refs), 1)
            self.assertEqual(paste.submit("next"), ConsoleMessage("next"))

    def test_prompt_is_anchored_above_the_bottom_toolbar(self):
        BottomAnchoredPromptSession, _ContextMeter, _read_console_input = _console()
        with create_pipe_input() as console_input:
            session = BottomAnchoredPromptSession(
                bottom_toolbar=lambda: "实际输入 0 / compact 200k  ·  compact 0 次\nSession ID：session-1",
                input=console_input,
                output=DummyOutput(),
            )

            root = session.layout.container
            self.assertIsInstance(root, HSplit)
            self.assertIsInstance(root.children[0], Window)
            prompt = root.children[1]
            self.assertIsInstance(prompt, HSplit)
            self.assertEqual(prompt.preferred_height(80, 24).min, 3)
            self.assertEqual(prompt.preferred_height(80, 24).max, 3)

    def test_streaming_output_renders_in_layout_until_final_delivery(self):
        from helperme.channels.tui.console import _StreamingConsoleOutput

        rendered = []
        written = []
        output = _StreamingConsoleOutput(
            lambda: rendered.append(output.render()),
            written.append,
        )

        output.preview("session-1", "started", "output-1", None)
        self.assertEqual(output.render(), "思考中")
        output.preview("session-1", "delta", "output-1", "你")
        output.preview("session-1", "delta", "output-1", "好")

        self.assertEqual(output.render(), "助手：你好")
        self.assertEqual(written, [])

        output.deliver("session-1", "output-1", "你好")

        self.assertEqual(output.render(), "")
        self.assertEqual(written, ["\n助手：你好"])
        self.assertEqual(rendered[-1], "")

    def test_aborted_stream_is_written_once_with_marker(self):
        from helperme.channels.tui.console import _StreamingConsoleOutput

        written = []
        output = _StreamingConsoleOutput(lambda: None, written.append)
        output.preview("session-1", "started", "output-1", None)
        output.preview("session-1", "delta", "output-1", "部分内容")

        output.preview("session-1", "aborted", "output-1", None)

        self.assertEqual(output.render(), "")
        self.assertEqual(
            written,
            ["\n助手：部分内容\n\n[输出已中止]"],
        )

    def test_context_meter_tracks_only_the_selected_session(self):
        _BottomAnchoredPromptSession, ContextMeter, _read_console_input = _console()
        meter = ContextMeter()
        meter.select(ConversationStatus("chat", "session-1", 0, None), 200_000)

        def rendered(context):
            return f"{context}  ·  compact 0 次\nSession ID：session-1"

        meter.update("another-session", 90_000, 200_000)
        self.assertEqual(meter.render(), rendered("实际输入 0 / compact 200k"))

        meter.update("chat", 12_345, 200_000)
        self.assertEqual(meter.render(), rendered("实际输入 12.3k / compact 200k"))

        meter.update_subagent_activity("another-session", True)
        self.assertEqual(meter.render(), rendered("实际输入 12.3k / compact 200k"))

        meter.update_subagent_activity("chat", True)
        self.assertEqual(
            meter.render(),
            "实际输入 12.3k / compact 200k  ·  compact 0 次  ·  子 Agent 工作中\nSession ID：session-1",
        )

        meter.update_subagent_activity("chat", False)
        self.assertEqual(meter.render(), rendered("实际输入 12.3k / compact 200k"))

        meter.update_conversation_status(ConversationStatus("other", "other", 9, "running"))
        self.assertEqual(meter.render(), rendered("实际输入 12.3k / compact 200k"))
        meter.update_conversation_status(ConversationStatus("chat", "session-1", 0, "running"))
        self.assertIn("compact 整理中", meter.render())
        meter.update_conversation_status(ConversationStatus("chat", "session-1", 0, "ready"))
        self.assertIn("compact 等待切换", meter.render())
        meter.update_conversation_status(ConversationStatus("chat", "session-2", 1, None))
        self.assertEqual(meter.render(), "实际输入 0 / compact 200k  ·  compact 1 次\nSession ID：session-2")
        meter.update("chat", 2_000, 200_000)
        self.assertIn("实际输入 2k / compact 200k", meter.render())

    async def test_reader_continuously_collects_complete_lines(self):
        _BottomAnchoredPromptSession, _ContextMeter, read_console_input = _console()
        queue: asyncio.Queue[ConsoleMessage | None] = asyncio.Queue()
        session = AsyncMock()
        session.prompt_async.side_effect = (
            "第一个任务",
            "运行时打断",
            EOFError,
        )

        with patch("helperme.channels.tui.console.patch_stdout") as patched:
            await read_console_input(queue, session)

        patched.assert_called_once_with()
        self.assertEqual(
            session.prompt_async.await_args_list,
            [
                call("你：", refresh_interval=0.25),
                call("你：", refresh_interval=0.25),
                call("你：", refresh_interval=0.25),
            ],
        )
        self.assertEqual(await queue.get(), ConsoleMessage("第一个任务"))
        self.assertEqual(await queue.get(), ConsoleMessage("运行时打断"))
        self.assertIsNone(await queue.get())
