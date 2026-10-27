from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from konden.assistant.shared_memory import (
    READ_SHARED_MEMORY, UPDATE_SHARED_MEMORY,
    SharedMemoryStore, UpdateSharedMemory, shared_memory_bindings,
)
from konden.assistant.agents import AgentCreated, AgentRecord
from konden.assistant.personal_memory import (
    READ_PERSONAL_MEMORY, UPDATE_PERSONAL_MEMORY, personal_memory_store, personal_memory_bindings,
)
from konden.paths import KondenHome
from konden.runtime import AgentRuntime, DomainFactCommitted, StateProjector
from konden.assistant.context.projection import ModelContextProjector
from tests.fixtures.artifacts import MemoryArtifactGateway
from tests.fixtures.memory_journal import MemoryJournal
from konden.assistant.agents import fact_arguments
from konden.assistant.assembly import build_assistant_assembly
from konden.assistant.workspaces import directory_binding
from konden.config import AssistantConfig
from konden.llm.api import LLMCallResult, LLMResponse, LLMUsage
from tests.fixtures.workspaces import workspace_record
from tests.session_scheduler import SettlingScheduler


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
                author=name, source=f"bot:{name}", operation_id="first",
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

    async def test_memory_observations_and_captured_prefixes_are_excluded_from_history(self):
        runtime = AgentRuntime(MemoryJournal(), None, {})
        await runtime.create_session("lin")
        await runtime.receive_domain_fact(
            "lin", "bot.shared_memory", {"version": 1, "content": "旧观察正文", "updated_by": "用户"},
            source="bot.shared_memory", delivery_id="observed", requests_decision=False,
        )
        await runtime.receive_user_message("lin", "当前问题", delivery_id="question")
        events = await runtime.snapshot("lin")
        projected = ModelContextProjector(MemoryArtifactGateway()).prepare(
            events, StateProjector().project_visible("lin", events), "lin",
            prefix=[{"role": "user", "content": "<shared_memory>旧前言正文</shared_memory>"},
                    {"role": "user", "content": "<working_memory>旧加载正文</working_memory>"}],
        )
        self.assertIn("当前问题", str(projected.messages))
        for text in ("旧观察正文", "旧前言正文", "旧加载正文"):
            self.assertNotIn(text, str(projected.messages))

    async def test_personal_memory_is_scoped_to_bot_and_enforces_its_smaller_limit(self):
        home = KondenHome(Path(self.directory.name))
        ada = AgentRecord("ada", AgentCreated(name="Ada", directory=str(home.agent_directories_root / "ada")))
        lin = AgentRecord("lin", AgentCreated(name="Lin", directory=str(home.agent_directories_root / "lin")))
        personal = personal_memory_store(home, ada)
        tools = personal_memory_bindings(personal, ada.name)
        context = SimpleNamespace(session_id="ada", command_id="personal")
        saved = await tools[UPDATE_PERSONAL_MEMORY].handler(context, {"version": 0, "content": "🐼" * 200})
        self.assertEqual(saved["code"], "PERSONAL_MEMORY_UPDATED")
        self.assertEqual(personal_memory_store(home, ada.renamed("Aster")).read().model_dump(), saved["data"])
        self.assertEqual(personal_memory_store(home, lin).read().content, "")
        self.assertEqual(self.store.read().content, "")
        too_long = await tools[UPDATE_PERSONAL_MEMORY].handler(context, {"version": 1, "content": "字" * 201})
        self.assertEqual(too_long["code"], "PERSONAL_MEMORY_TOO_LONG")
        self.assertEqual(personal.read().model_dump(), saved["data"])
        for name in (READ_PERSONAL_MEMORY, UPDATE_PERSONAL_MEMORY):
            result = await tools[name].handler(context, {"bot": "Lin", "version": 1, "content": "改别人的约束"})
            self.assertEqual(result["code"], "INVALID_ARGUMENT")
        self.assertTrue(tools[UPDATE_PERSONAL_MEMORY].requires_authorization)

    async def test_current_memories_are_loaded_at_request_tail_without_rewriting_history(self):
        home = KondenHome(Path(self.directory.name))
        directory = home.agent_directories_root / "ada"
        directory.mkdir(parents=True)
        ada = AgentRecord("ada", AgentCreated(name="Ada", directory=str(directory)))
        shared = SharedMemoryStore(home.state_root / "shared_memory.sqlite")
        shared.update(UpdateSharedMemory(version=0, content="旧约束：解释过程后给结论。"),
                      author="用户", source="user", operation_id="first")
        own = personal_memory_store(home, ada)
        tools = personal_memory_bindings(own, ada.name)
        await tools[UPDATE_PERSONAL_MEMORY].handler(SimpleNamespace(session_id="ada", command_id="own"),
                                                  {"version": 0, "content": "不确定时明确说明缺少的依据。"})
        requests = []

        class Llm:
            async def chat(self, messages, model, **kwargs):
                requests.append(messages)
                return LLMCallResult(LLMResponse(content="done"), LLMUsage(input_tokens=1, output_tokens=1))

        journal = MemoryJournal()
        runtime = AgentRuntime(journal, None, {})
        await runtime.create_session("ada")
        binding = directory_binding(directory)
        await runtime.receive_domain_fact("ada", binding.fact_type, dict(binding.data), source="workspace", delivery_id="directory")
        await runtime.receive_domain_fact("ada", **fact_arguments(ada.identity, delivery_id="identity"))
        assembly = await build_assistant_assembly(
            AssistantConfig(model_name="test", compact_threshold_tokens=200000, llm=Llm()),
            lambda *_args: None, journal, session_id="ada", workspace=workspace_record(directory),
            home=home, scheduler_factory=SettlingScheduler,
        )
        decide = assembly.runtime.step_runner._decision_maker.decide
        frames = []

        async def remember_frame(frame):
            frames.append(frame)
            return await decide(frame)

        assembly.runtime.step_runner._decision_maker.decide = remember_frame
        try:
            await assembly.runtime.receive_user_message("ada", "开始讨论。", delivery_id="first")
            await assembly.scheduler.wake("ada")
            await assembly.scheduler.join()
            first_events = await journal.snapshot("ada")
            baseline = requests[-1]
            self.assertIn("<working_memory>", baseline[-1]["content"])
            self.assertIn("旧约束：解释过程后给结论。", baseline[-1]["content"])
            shared.update(UpdateSharedMemory(version=1, content="新约束：先给结论，再解释依据。"),
                          author="用户", source="user", operation_id="second")
            self.assertEqual(await journal.snapshot("ada"), first_events)
            await decide(frames[-1])
            self.assertEqual(requests[-1][:-1], baseline[:-1])
            self.assertIn("新约束：先给结论，再解释依据。", requests[-1][-1]["content"])
            self.assertIn("不确定时明确说明缺少的依据。", requests[-1][-1]["content"])
            self.assertNotIn("旧约束：解释过程后给结论。", str(requests[-1]))
            self.assertFalse(any(isinstance(event.payload, DomainFactCommitted)
                                 and event.payload.fact_type in {"bot.shared_memory", "bot.personal_memory"}
                                 for event in await journal.snapshot("ada")))
        finally:
            await assembly.scheduler.close()
