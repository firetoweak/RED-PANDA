from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, call, patch

from redpanda.assistant.host.auto_authorize import AutoAuthorizeStore
from redpanda.assistant.host.supervisor import HostSupervisor


class HostAutoAuthorizeTest(unittest.IsolatedAsyncioTestCase):
    def host(self, root=None):
        host = object.__new__(HostSupervisor)
        host._auto_authorize = AutoAuthorizeStore(root)
        host._auto_authorize_lock = asyncio.Lock()
        host.workers = {"a": object(), "b": object()}
        host.request = AsyncMock()
        return host

    def test_global_opt_out_survives_reload_and_repeated_save_does_not_write(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            policy = AutoAuthorizeStore(root)
            self.assertTrue(policy.enabled)
            policy.set(False)
            self.assertFalse(AutoAuthorizeStore(root).enabled)
            with patch("redpanda.assistant.host.auto_authorize.write_json") as write:
                policy.set(False)
            write.assert_not_called()

    def test_failed_replace_preserves_previous_policy_in_memory_and_on_disk(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            policy = AutoAuthorizeStore(root)
            policy.set(False)
            with patch("redpanda.config.os.replace", side_effect=OSError("disk failure")):
                with self.assertRaisesRegex(OSError, "disk failure"):
                    policy.set(True)
            self.assertFalse(policy.enabled)
            self.assertFalse(AutoAuthorizeStore(root).enabled)
            self.assertEqual(list(root.iterdir()), [root / "authorization.json"])

    def test_corrupt_global_policy_is_not_treated_as_enabled(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "authorization.json").write_text('{"enabled": "false"}', encoding="utf-8")
            with self.assertRaises(ValueError):
                AutoAuthorizeStore(root)

    async def test_one_setting_reaches_all_active_workers_without_loading_history(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            host = self.host(root)
            await host.set_auto_authorize(False)
            self.assertEqual(host.request.await_args_list, [
                call("apply_auto_authorize", "a", {"enabled": False}),
                call("apply_auto_authorize", "b", {"enabled": False}),
            ])
            self.assertEqual(json.loads((root / "authorization.json").read_text()), {"enabled": False})

    async def test_overlapping_switches_reach_workers_in_the_same_order(self):
        host = self.host()
        entered, release = asyncio.Event(), asyncio.Event()
        applied = []

        async def apply(operation, session_id, arguments):
            if not arguments["enabled"]:
                entered.set()
                await release.wait()
            applied.append((session_id, arguments["enabled"]))

        host.request.side_effect = apply
        first = asyncio.create_task(host.set_auto_authorize(False))
        await entered.wait()
        second = asyncio.create_task(host.set_auto_authorize(True))
        await asyncio.sleep(0)
        self.assertFalse(host.auto_authorize())
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(applied, [("a", False), ("b", False), ("a", True), ("b", True)])
        self.assertTrue(host.auto_authorize())

    async def test_failed_worker_does_not_skip_others_and_retry_resynchronizes(self):
        host = self.host()
        entered, release = asyncio.Event(), asyncio.Event()

        async def apply(operation, session_id, arguments):
            if session_id == "a":
                raise RuntimeError("worker failed")
            entered.set()
            await release.wait()

        host.request.side_effect = apply
        update = asyncio.create_task(host.set_auto_authorize(False))
        await entered.wait()
        await asyncio.sleep(0)
        self.assertFalse(update.done())
        release.set()
        with self.assertRaises(ExceptionGroup):
            await update
        self.assertFalse(host.auto_authorize())
        host.request = AsyncMock()
        await host.set_auto_authorize(False)
        self.assertEqual(host.request.await_count, 2)
