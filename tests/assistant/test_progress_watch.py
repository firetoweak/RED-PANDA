import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from redpanda.assistant.artifacts import MemoryArtifactGateway
from redpanda.assistant.progress_watch import ProgressWatch, build_state, chain_identity, apply_intervention, QUESTION, PAUSED
from redpanda.runtime import UserMessageReceived
from tests.assistant.test_loop_guard import event
from tests.llm.test_jev import settings


class ProgressWatchTest(unittest.IsolatedAsyncioTestCase):
    async def start_watch(self, verdict, *, timeout=0.1):
        self.events = [event(1)]
        self.alive = True
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.applied = asyncio.Event()

        async def evaluate(*args):
            self.started.set()
            await self.release.wait()
            if isinstance(verdict, Exception):
                raise verdict
            return verdict

        async def apply(*args):
            self.calls.append(args)
            self.applied.set()

        self.calls = []
        self.jev = SimpleNamespace(settings=lambda: settings(timeout_seconds=timeout), evaluate=AsyncMock(side_effect=evaluate))
        watch = ProgressWatch(
            SimpleNamespace(snapshot=AsyncMock(side_effect=lambda _: tuple(self.events))), "s",
            active=lambda: True, scope=lambda: 0, apply=apply, store=MemoryArtifactGateway(),
            jev=self.jev, poll_seconds=0,
        )
        task = asyncio.create_task(watch.run(lambda: self.alive))
        await asyncio.sleep(0)
        self.events.extend((event(2), event(3)))
        await asyncio.wait_for(self.started.wait(), 2)
        return task

    async def finish(self, task):
        self.alive = False
        self.release.set()
        await asyncio.wait_for(task, 2)

    async def test_inflight_check_does_not_accumulate_requests_and_continue_suppresses_mechanical_hit(self):
        task = await self.start_watch(SimpleNamespace(choice="continue"))
        self.events.append(event(4))
        await asyncio.sleep(0)
        self.assertEqual(self.jev.evaluate.await_count, 1)
        await self.finish(task)
        self.assertEqual(self.calls, [])

    async def test_known_unavailability_and_timeout_fall_back_to_mechanical_reminder(self):
        for timeout, release in ((0.1, True), (0.01, False)):
            task = await self.start_watch(None, timeout=timeout)
            if release:
                self.release.set()
            await asyncio.wait_for(self.applied.wait(), 2)
            await self.finish(task)
            self.assertEqual(self.calls[0][0], "remind")

    async def test_internal_failure_is_not_mechanical_fallback(self):
        task = await self.start_watch(RuntimeError("invariant"))
        self.release.set()
        with self.assertRaisesRegex(RuntimeError, "invariant"):
            await task
        self.assertEqual(self.calls, [])

    def test_bounded_state_keeps_batches_and_latest_user_correction(self):
        user = replace(event(1), payload=UserMessageReceived("目标：修复中文路径。" * 5000))
        correction = replace(event(9), payload=UserMessageReceived("最新要求：保留用户文件。"))
        records = (user, *(event(i, ("read",) * 8) for i in range(2, 9)), correction)
        result = json.loads(build_state(records, MemoryArtifactGateway(), recent_steps=7, max_tokens=1024))
        self.assertIn("最新要求", result["task_requirements"])
        self.assertGreater(result["omitted_steps"], 0)
        self.assertTrue(all(len(step["actions"]) == 8 for step in result["recent_steps"]))
        self.assertNotIn("progress", result)
        self.assertTrue(QUESTION["instructions"].isascii())
        self.assertTrue(all(text.isascii() for text in QUESTION["criteria"].values()))

    async def test_new_input_and_worker_generation_reject_delayed_intervention(self):
        worker = SimpleNamespace(running=True, stopping=False, run_revision=0)
        records = [replace(event(1), payload=UserMessageReceived("old")), event(2)]
        identity = chain_identity(records)
        host = SimpleNamespace(
            turn_locks={}, workers={"s": worker}, closed=False,
            is_paused=lambda _: False, store=SimpleNamespace(require=lambda _: "unused"),
            request=AsyncMock(), _cancel_turn=AsyncMock(), system_notice_sink=None,
        )
        with patch("redpanda.assistant.progress_watch.SqliteJournal", return_value=SimpleNamespace(
            snapshot=AsyncMock(side_effect=lambda _: tuple(records))
        )):
            records.append(replace(event(3), payload=UserMessageReceived("new")))
            await apply_intervention(host, "s", worker, "pause", identity, "event-2", 0)
            records.pop()
            worker.run_revision = 1
            await apply_intervention(host, "s", worker, "pause", identity, "event-2", 0)
            worker.run_revision = 0
            host.workers["s"] = object()
            await apply_intervention(host, "s", worker, "remind", identity, "event-2", 0)
        host._cancel_turn.assert_not_awaited()
        host.request.assert_not_awaited()

    async def test_pause_uses_existing_interrupt_then_records_warning(self):
        worker = SimpleNamespace(running=True, stopping=False, run_revision=0)
        records = [replace(event(1), payload=UserMessageReceived("task")), event(2)]
        order = []
        async def cancel(_):
            order.append("cancel_turn")
        async def request(*args):
            order.append(args)
        host = SimpleNamespace(
            turn_locks={}, workers={"s": worker}, closed=False,
            is_paused=lambda _: False, store=SimpleNamespace(require=lambda _: "unused"),
            request=request, _cancel_turn=cancel, system_notice_sink=None,
        )
        with patch("redpanda.assistant.progress_watch.SqliteJournal", return_value=SimpleNamespace(snapshot=AsyncMock(return_value=tuple(records)))):
            await apply_intervention(host, "s", worker, "pause", chain_identity(records), "event-2", 0)
        self.assertEqual(order[0], "cancel_turn")
        self.assertEqual(order[1][2]["data"]["text"], PAUSED)
        self.assertFalse(order[1][2]["requests_decision"])
