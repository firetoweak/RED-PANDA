from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from redpanda.assistant.compact.core import window_preamble
from redpanda.assistant.shared_memory import (
    READ_SHARED_MEMORY, SHARED_MEMORY, UPDATE_SHARED_MEMORY,
    SharedMemoryStore, UpdateSharedMemory, project_shared_memory,
    shared_memory_bindings, sync_shared_memory,
)
from redpanda.runtime import AgentRuntime, StateProjector
from tests.fixtures.memory_journal import MemoryJournal
from tests.fixtures.projection import project_chat_messages


class SharedMemoryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "shared_memory.sqlite"
        self.store = SharedMemoryStore(self.path)
        self.tools = shared_memory_bindings(self.store, "Ada")

    async def update(self, content, version=0, command="write"):
        context = SimpleNamespace(session_id="ada", command_id=command)
        return await self.tools[UPDATE_SHARED_MEMORY].handler(context, {"version": version, "content": content})

    async def test_length_limit_rejects_instead_of_truncating_and_empty_clears(self):
        # Chinese, Latin, punctuation, whitespace and line breaks all count.
        content = "约a。 \n" * 100
        saved = await self.update(content)
        self.assertTrue(saved["ok"])
        for too_long in (content + "字", " " * 501):
            result = await self.update(too_long, version=1, command="too-long")
            self.assertEqual(result["code"], "SHARED_MEMORY_TOO_LONG")
            self.assertEqual(SharedMemoryStore(self.path).read().content, content)
        cleared = await self.update("", version=1, command="clear")
        self.assertEqual(cleared["data"]["content"], "")
        self.assertEqual(cleared["data"]["version"], 2)

    async def test_concurrent_bots_cannot_overwrite_a_change_they_have_not_read(self):
        other = SharedMemoryStore(self.path)
        with ThreadPoolExecutor(2) as workers:
            jobs = [workers.submit(
                store.update, UpdateSharedMemory(version=0, content=name),
                author=name, session_id=name, command_id="first",
            ) for store, name in ((self.store, "Ada"), (other, "Lin"))]
            results = [job.result() for job in jobs]
        winner, = [result for result in results if result["ok"]]
        loser, = [result for result in results if not result["ok"]]
        self.assertEqual(loser["code"], "SHARED_MEMORY_CHANGED")
        self.assertEqual(loser["data"], winner["data"])
        self.assertEqual(other.read().model_dump(), winner["data"])

    async def test_repeating_a_committed_update_keeps_its_result_and_provenance(self):
        saved = await self.update("服务归属不明时先找同事确认。")
        await self.update("共享资源先协调。", version=1, command="second")
        self.assertEqual(await self.update("服务归属不明时先找同事确认。"), saved)
        read = await shared_memory_bindings(SharedMemoryStore(self.path), "Lin")[READ_SHARED_MEMORY].handler(None, {})
        self.assertEqual(read["data"], {
            "version": 2, "content": "共享资源先协调。", "updated_by": "Ada",
        })
        self.assertTrue(self.tools[UPDATE_SHARED_MEMORY].requires_authorization)

    async def test_observations_are_frozen_append_only_and_reappear_after_compact(self):
        runtime = AgentRuntime(MemoryJournal(), None, {})
        await runtime.create_session("lin")
        await sync_shared_memory(runtime, "lin", self.store)
        initial = await runtime.snapshot("lin")
        self.assertEqual(project_shared_memory(initial)["content"], "")
        await self.update("启动服务前检查端口，归属不明的进程不要停止。")
        # Editing the shared note does not alter a colleague's existing view.
        self.assertEqual(await runtime.snapshot("lin"), initial)
        await sync_shared_memory(runtime, "lin", self.store)
        observed = await runtime.snapshot("lin")
        self.assertEqual(observed[:len(initial)], initial)
        self.assertFalse(observed[-1].payload.requests_decision)
        self.assertEqual(observed[-1].payload.fact_type, SHARED_MEMORY)
        await sync_shared_memory(runtime, "lin", self.store)
        self.assertEqual(await runtime.snapshot("lin"), observed)
        messages = project_chat_messages(observed, StateProjector().project_visible("lin", observed))
        note = messages[-1]
        self.assertIn("<shared_memory>", note["content"])
        compacted = window_preamble("交接", len(observed), None, None, project_shared_memory(observed))
        self.assertEqual(compacted[-1], note)
        self.assertIn("启动服务前检查端口", note["content"])
