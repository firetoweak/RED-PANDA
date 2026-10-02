from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

from helperme.assistant.host.supervisor import HostSupervisor
from helperme.assistant.session_metadata import SessionFlagStore


class HostRetryTest(unittest.IsolatedAsyncioTestCase):
    async def test_retry_clears_pause_otherwise_resumes(self):
        host = object.__new__(HostSupervisor)
        host.models = None
        host._pause = SessionFlagStore(None, "paused.json")
        host.set_paused = AsyncMock(return_value="unpaused")
        host.resume = AsyncMock(return_value="resumed")

        self.assertEqual(await host.retry("session-1"), "resumed")
        host.resume.assert_awaited_once_with("session-1")
        host.set_paused.assert_not_awaited()

        host._pause.remember("session-1", True)
        self.assertEqual(await host.retry("session-1"), "unpaused")
        host.set_paused.assert_awaited_once_with("session-1", False)


class HostAcceptInputPauseTest(unittest.IsolatedAsyncioTestCase):
    async def test_accept_input_clears_host_pause_without_copying_worker(self):
        host = object.__new__(HostSupervisor)
        host.models = None
        host._pause = SessionFlagStore(None, "paused.json")
        host._pause.remember("session-1", True)
        view = SimpleNamespace(paused=True)
        host.compact = SimpleNamespace(application=AsyncMock(return_value=view))
        host._with_host_metadata = lambda observed, session_id: observed

        result = await host.accept_input("session-1", "hello")

        self.assertIs(result, view)
        self.assertFalse(host._pause.get("session-1"))
        host.compact.application.assert_awaited_once_with(
            "accept_input",
            "session-1",
            {"content": "hello"},
        )


class HostResumePauseTest(unittest.IsolatedAsyncioTestCase):
    async def test_resume_views_when_paused_otherwise_resumes(self):
        host = object.__new__(HostSupervisor)
        host.models = None
        host._pause = SessionFlagStore(None, "paused.json")
        host.compact = SimpleNamespace(application=AsyncMock(return_value="view"))
        host._with_host_metadata = lambda observed, session_id: observed

        self.assertEqual(await host.resume("session-1"), "view")
        host.compact.application.assert_awaited_once_with("resume", "session-1", {})

        host.compact.application.reset_mock()
        host._pause.remember("session-1", True)
        self.assertEqual(await host.resume("session-1"), "view")
        host.compact.application.assert_awaited_once_with("view", "session-1", {})

    async def test_set_paused_does_not_forward_to_worker(self):
        host = object.__new__(HostSupervisor)
        host.models = None
        host._pause = SessionFlagStore(None, "paused.json")
        host.compact = SimpleNamespace(application=AsyncMock(return_value="held"))
        host._with_host_metadata = lambda observed, session_id: observed

        self.assertEqual(await host.set_paused("session-1", True), "held")
        self.assertTrue(host.is_paused("session-1"))
        host.compact.application.assert_awaited_once_with("view", "session-1", {})

        host.compact.application.reset_mock()
        self.assertEqual(await host.set_paused("session-1", False), "held")
        self.assertFalse(host.is_paused("session-1"))
        host.compact.application.assert_awaited_once_with("resume", "session-1", {})

    async def test_user_message_clears_pause_without_writing_default(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            host = object.__new__(HostSupervisor)
            host.models = None
            host._pause = SessionFlagStore(root, "paused.json")
            host.compact = SimpleNamespace(application=AsyncMock())

            await host.receive_user_message("session-1", "hello")
            self.assertFalse(host.is_paused("session-1"))
            self.assertFalse((root / "paused.json").is_file())

            host._pause.set("session-1", True)
            await host.receive_user_message("session-1", "go on")
            self.assertFalse(host.is_paused("session-1"))
            self.assertEqual(
                json.loads((root / "paused.json").read_text(encoding="utf-8")),
                {"session-1": False},
            )
