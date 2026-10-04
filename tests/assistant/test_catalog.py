from types import SimpleNamespace
import unittest

from redpanda.assistant.catalog import (
    CATALOG,
    CapabilityCatalog,
    project_catalog,
)
from redpanda.assistant.context.projection import project_chat_messages
from redpanda.assistant.toolsets import ToolsetDescriptor
from redpanda.runtime import (
    AgentRuntime,
    MemoryJournal,
    DomainFactCommitted,
    StateProjector,
)


class CatalogTest(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_updates_are_append_only_and_restore_without_repetition(self):
        runtime = AgentRuntime(MemoryJournal(), None, {})
        await runtime.create_session("s")
        records = [
            SimpleNamespace(name="demo", description="first", revision=1, enabled=True)
        ]
        descriptors = [ToolsetDescriptor("mcp:z", "Z"), ToolsetDescriptor("mcp:a", "A")]
        surface = SimpleNamespace(
            registry_descriptors=lambda: tuple(descriptors),
            apply_catalog=lambda session_id, value: setattr(
                surface, "applied", (session_id, value)
            ),
        )
        skills = SimpleNamespace(
            registry_catalog=lambda: [
                {"id": r.name, "description": r.description, "revision": r.revision}
                for r in records
            ],
            apply_catalog=lambda session_id, value: setattr(
                skills, "applied", (session_id, value)
            ),
        )
        clis = SimpleNamespace(
            registry_catalog=lambda: [],
            apply_catalog=lambda session_id, value: setattr(
                clis, "applied", (session_id, value)
            ),
        )
        management = SimpleNamespace(catalog_instruction=lambda sid: "mcp / skill")
        catalog = CapabilityCatalog(surface, skills, clis, management)
        await catalog.sync(runtime, "s")
        initial = await runtime.snapshot("s")
        initial_revision = project_catalog(initial).revision
        descriptors.reverse()
        await catalog.sync(runtime, "s")
        self.assertEqual(await runtime.snapshot("s"), initial)
        records[0].description = "updated"
        records[0].revision = 2
        await catalog.sync(runtime, "s")
        updated = await runtime.snapshot("s")
        self.assertEqual(updated[: len(initial)], initial)
        self.assertEqual(len(updated), 2)
        self.assertTrue(
            all(
                isinstance(e.payload, DomainFactCommitted)
                and e.payload.fact_type == CATALOG
                for e in updated
            )
        )
        projector = StateProjector()
        before = project_chat_messages(initial, projector.project_visible("s", initial))
        after = project_chat_messages(updated, projector.project_visible("s", updated))
        self.assertEqual(after[: len(before)], before)
        self.assertIn("<capability_catalog>", after[-1]["content"])
        self.assertNotEqual(project_catalog(updated).revision, initial_revision)
        await catalog.sync(runtime, "s")
        self.assertEqual(await runtime.snapshot("s"), updated)

        records[0].description = "first"
        records[0].revision = 1
        await catalog.sync(runtime, "s")
        restored_value = await runtime.snapshot("s")
        self.assertEqual(len(restored_value), 3)
        self.assertEqual(project_catalog(restored_value).revision, initial_revision)
        self.assertEqual(
            len({event.delivery.delivery_id for event in restored_value}),
            3,
        )

        restored_surface = SimpleNamespace(
            apply_catalog=lambda session_id, value: setattr(
                restored_surface, "applied", (session_id, value)
            )
        )
        restored_skills = SimpleNamespace(
            apply_catalog=lambda session_id, value: setattr(
                restored_skills, "applied", (session_id, value)
            )
        )
        restored_clis = SimpleNamespace(
            apply_catalog=lambda session_id, value: setattr(
                restored_clis, "applied", (session_id, value)
            )
        )
        restored = CapabilityCatalog(
            restored_surface,
            restored_skills,
            restored_clis,
            management,
        )
        restored.rehydrate("s", restored_value)
        self.assertEqual(restored_surface.applied[1][0].id, "mcp:a")
        self.assertEqual(restored_skills.applied[1][0].revision, 1)
