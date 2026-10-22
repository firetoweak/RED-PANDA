from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.assistant.session_metadata import SessionFlagStore


class HostAcceptInputStopTest(unittest.IsolatedAsyncioTestCase):
    async def test_accept_input_clears_host_stop_without_copying_worker(self):
        host = object.__new__(HostSupervisor)
        host.models = None
        host._execution_stopped = SessionFlagStore(None, "execution_stopped.json")
        host._execution_stopped.remember("session-1", True)
        host.turn_locks = {}
        view = SimpleNamespace(execution_stopped=True)
        host.compact = SimpleNamespace(application=AsyncMock(return_value=view))
        host._with_host_metadata = lambda observed, session_id: observed

        result = await host.accept_input("session-1", "hello", delivery_id="hello")

        self.assertIs(result, view)
        self.assertFalse(host._execution_stopped.get("session-1"))


class HostResumeStopTest(unittest.IsolatedAsyncioTestCase):
    async def test_user_message_clears_stop_without_writing_default(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            host = object.__new__(HostSupervisor)
            host.models = None
            host.turn_locks = {}
            host._execution_stopped = SessionFlagStore(root, "execution_stopped.json")
            host.compact = SimpleNamespace(application=AsyncMock())

            await host.receive_user_message("session-1", "hello")
            self.assertFalse(host.is_execution_stopped("session-1"))
            self.assertFalse((root / "execution_stopped.json").is_file())

            host._execution_stopped.set("session-1", True)
            await host.receive_user_message("session-1", "go on")
            self.assertFalse(host.is_execution_stopped("session-1"))
            self.assertEqual(
                json.loads((root / "execution_stopped.json").read_text(encoding="utf-8")),
                {"session-1": False},
            )


class ExecutionStopRenameTest(unittest.IsolatedAsyncioTestCase):
    async def test_existing_stop_survives_metadata_file_rename_and_restart(self):
        from redpanda.assistant.host.session_store import SessionStore
        from redpanda.paths import RedPandaHome

        with TemporaryDirectory() as directory:
            home = RedPandaHome(Path(directory))
            store = SessionStore(home.runtime_sessions_root)
            SessionFlagStore(store.root, "paused.json").set("session-1", True)
            for _ in range(2):
                host = HostSupervisor(store, lambda: None, home, lambda *_: None, llm=None)
                try:
                    self.assertTrue(host.is_execution_stopped("session-1"))
                    self.assertFalse((store.root / "paused.json").exists())
                    self.assertTrue((store.root / "execution_stopped.json").exists())
                finally:
                    await host.close()
