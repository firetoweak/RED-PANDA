import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from konden.assistant.context.memory import MemoryContext, memory_observations, SHARED_MEMORY, PERSONAL_MEMORY

from konden.assistant.shared_memory import (
    READ_SHARED_MEMORY, UPDATE_SHARED_MEMORY,
    SharedMemoryStore, UpdateSharedMemory, shared_memory_bindings,
)
from konden.assistant.agents import AgentCreated, AgentRecord
from konden.assistant.personal_memory import (
    READ_PERSONAL_MEMORY, UPDATE_PERSONAL_MEMORY, personal_memory_store, personal_memory_bindings, UpdatePersonalMemory,
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

    async def test_observations_are_passive_append_only_and_never_own_current_memory(self):
        home = KondenHome(Path(self.directory.name))
        ada = AgentRecord("ada", AgentCreated(name="Ada", directory=str(home.agent_directories_root / "ada")))
        own = personal_memory_store(home, ada)
        observer = MemoryContext(self.store, own)
        runtime = AgentRuntime(MemoryJournal(), None, {})
        await runtime.create_session("ada")
        await observer.observe(runtime, "ada")
        initial = await runtime.snapshot("ada")
        self.assertEqual(set(memory_observations(initial)), {SHARED_MEMORY, PERSONAL_MEMORY})
        self.assertTrue(all(not e.payload.requests_decision for e in initial
                            if isinstance(e.payload, DomainFactCommitted)))
        await observer.observe(runtime, "ada")
        self.assertEqual(await runtime.snapshot("ada"), initial)
        saved = await self.update("先确认共享资源的使用者。")
        self.assertEqual(await runtime.snapshot("ada"), initial)
        self.assertEqual(memory_observations(initial)[SHARED_MEMORY]["content"], "")
        # Historical projection has no access to the current memory stores.
        projector = ModelContextProjector(MemoryArtifactGateway())
        def project(events):
            return projector.prepare(events, StateProjector().project_visible("ada", events), "ada").messages
        before = project(initial)
        await observer.observe(runtime, "ada")
        observed = await runtime.snapshot("ada")
        self.assertEqual(observed[:len(initial)], initial)
        self.assertEqual(project(observed)[:len(before)], before)
        self.assertEqual(project(initial), before)
        self.assertEqual(self.store.read().model_dump(), saved["data"])
        self.assertEqual(memory_observations(observed)[SHARED_MEMORY], saved["data"])

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

    async def test_consecutive_decisions_keep_prefix_through_memory_updates_and_clear(self):
        home = KondenHome(Path(self.directory.name))
        directory = home.agent_directories_root / "ada"
        directory.mkdir(parents=True)
        ada = AgentRecord("ada", AgentCreated(name="Ada", directory=str(directory)))
        shared = SharedMemoryStore(home.state_root / "shared_memory.sqlite")
        shared.update(UpdateSharedMemory(version=0, content="Explain before concluding."),
                      author="user", source="user", operation_id="first")
        own = personal_memory_store(home, ada)
        own.update(UpdatePersonalMemory(version=0, content="State missing evidence."),
                   author="user", source="user", operation_id="own")
        requests = []
        journal = MemoryJournal()

        class Llm:
            async def chat(self, messages, model, **kwargs):
                # Each presented body must already have an exact committed observation.
                events = await journal.snapshot("ada")
                for message in messages:
                    if str(message["content"]).startswith("<working_memory>"):
                        data = json.loads(message["content"].split("\n")[1])
                        assert any(isinstance(e.payload, DomainFactCommitted)
                                   and e.payload.fact_type == data["fact"]
                                   and dict(e.payload.data) == data["data"] for e in events)
                requests.append(messages)
                return LLMCallResult(LLMResponse(content="done"), LLMUsage(input_tokens=1, output_tokens=1))

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

        async def turn(text):
            await assembly.runtime.receive_user_message("ada", text, delivery_id=text)
            await assembly.scheduler.wake("ada")
            await assembly.scheduler.join()
            return requests[-1]

        def memories(messages):
            return [m for m in messages if str(m["content"]).startswith("<working_memory>")]

        try:
            first = await turn("first")
            second = await turn("second")
            third = await turn("third")
            self.assertEqual(second[:len(first)], first)
            self.assertEqual(third[:len(second)], second)
            self.assertEqual(len(memories(third)), 2)
            before_update = await journal.snapshot("ada")
            shared.update(UpdateSharedMemory(version=1, content="Conclude before explaining."),
                          author="user", source="user", operation_id="second")
            self.assertEqual(await journal.snapshot("ada"), before_update)
            # Reusing the same frozen decision must not read newer product data.
            await decide(frames[-1])
            self.assertEqual(requests[-1], third)
            changed = await turn("changed")
            self.assertEqual(changed[:len(third)], third)
            self.assertEqual(len(memories(changed)), 3)
            self.assertIn("Conclude before explaining.", memories(changed)[-1]["content"])
            self.assertIn("Explain before concluding.", str(changed))
            own.update(UpdatePersonalMemory(version=1, content=""),
                       author="user", source="user", operation_id="clear")
            cleared = await turn("cleared")
            self.assertEqual(cleared[:len(changed)], changed)
            self.assertEqual(len(memories(cleared)), 4)
            self.assertIn('"content": ""', memories(cleared)[-1]["content"])
            unchanged = await turn("unchanged")
            self.assertEqual(unchanged[:len(cleared)], cleared)
            self.assertEqual(memories(unchanged), memories(cleared))
            observed = memory_observations(await journal.snapshot("ada"))
            self.assertEqual(observed[SHARED_MEMORY], shared.read().model_dump())
            self.assertEqual(observed[PERSONAL_MEMORY], own.read().model_dump())
        finally:
            await assembly.scheduler.close()
