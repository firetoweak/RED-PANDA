from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.assistant.session_metadata import SessionFlagStore


class HostAcceptInputPauseTest(unittest.IsolatedAsyncioTestCase):
    async def test_accept_input_clears_host_pause_without_copying_worker(self):
        host = object.__new__(HostSupervisor)
        host.models = None
        host._pause = SessionFlagStore(None, "paused.json")
        host._pause.remember("session-1", True)
        host.turn_locks = {}
        view = SimpleNamespace(paused=True)
        host.compact = SimpleNamespace(application=AsyncMock(return_value=view))
        host._with_host_metadata = lambda observed, session_id: observed

        result = await host.accept_input("session-1", "hello")

        self.assertIs(result, view)
        self.assertFalse(host._pause.get("session-1"))


class HostResumePauseTest(unittest.IsolatedAsyncioTestCase):
    async def test_user_message_clears_pause_without_writing_default(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            host = object.__new__(HostSupervisor)
            host.models = None
            host.turn_locks = {}
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
