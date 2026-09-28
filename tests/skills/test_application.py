import tempfile
import unittest
from pathlib import Path

from helperme.paths import HelperMeHome
from helperme.llm.api import LLMTransientError
from helperme.skills.application import SkillApplicationService
from helperme.skills.console import SkillConsoleAdapter
from helperme.skills.models import SkillSourceRef
from tests.skills.test_package import write_skill


class RecordingSummarizer:
    def __init__(self, *, error=None):
        self.calls = []
        self.error = error

    async def summarize(self, candidate, old_main, new_main):
        self.calls.append((candidate, old_main, new_main))
        if self.error is not None:
            raise self.error
        return "Workflow changed; review scripts."


class SkillApplicationServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.workspace = HelperMeHome(root / ".helperme")
        self.workspace.initialize()
        self.source = root / "source"
        write_skill(self.source, name="demo", description="Demo skill")
        reference = self.source / "references" / "guide.md"
        reference.parent.mkdir()
        reference.write_text("guide", encoding="utf-8")
        self.service = SkillApplicationService(self.workspace)
        await self.service.install_local(self.source)

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_storage_has_independent_product_root(self):
        self.assertEqual(
            self.service.skills_root,
            self.workspace.skills_root.resolve(),
        )
        self.assertEqual(self.service.skills_root.parent, self.workspace.root)
        self.assertNotEqual(self.service.skills_root, self.workspace.mcp_root)

    async def test_test_enable_disable_and_remove(self):
        inspection = await self.service.test_skill("demo")
        self.assertEqual(inspection.record.name, "demo")
        self.assertEqual(
            [item[0] for item in inspection.files],
            ["SKILL.md", "references/guide.md"],
        )
        self.assertEqual(await self.service.test_skill("demo"), inspection)

        enabled = await self.service.set_enabled("demo", True)
        self.assertTrue(enabled.enabled)
        self.assertEqual(enabled.revision, 1)
        disabled = await self.service.set_enabled("demo", False)
        self.assertFalse(disabled.enabled)
        self.assertEqual(disabled.revision, 2)

        removed = await self.service.remove("demo")
        self.assertEqual(removed.name, "demo")
        self.assertIsNone(await self.service.registry.get("demo"))
        self.assertFalse(
            (self.service.skills_root / "packages" / "demo").exists()
        )

    async def test_enable_rejects_modified_installed_package(self):
        await self.service.set_enabled("demo", False)
        installed = self.service.skills_root / "packages" / "demo" / "SKILL.md"
        installed.write_text(
            "---\nname: demo\ndescription: Demo skill\n---\ntampered\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(RuntimeError, "hash"):
            await self.service.set_enabled("demo", True)

        self.assertFalse((await self.service.registry.get("demo")).enabled)

    async def test_enable_rejects_catalog_over_budget_without_state_change(self):
        await self.service.set_enabled("demo", False)
        constrained = SkillApplicationService(
            self.workspace,
            registry=self.service.registry,
            max_catalog_chars=5,
        )

        with self.assertRaisesRegex(ValueError, "SKILL_CATALOG_LIMIT"):
            await constrained.set_enabled("demo", True)

        self.assertFalse((await self.service.registry.get("demo")).enabled)

    async def test_update_applies_frozen_candidate_not_later_source_drift(self):
        write_skill(
            self.source,
            name="demo",
            description="Demo skill v2",
            body="candidate v2\n",
        )
        added = self.source / "scripts" / "new.py"
        added.parent.mkdir()
        added.write_text("print('v2')", encoding="utf-8")

        report = await self.service.check_update("demo")
        candidate = report.candidate
        self.assertIsNotNone(report.summary_error)
        self.assertTrue(candidate.diff.changed)
        self.assertEqual(candidate.operation, "update")
        self.assertEqual(candidate.diff.added, ("scripts/new.py",))
        self.assertIn("SKILL.md", candidate.diff.modified)
        machine_diff = candidate.diff.to_dict()
        self.assertTrue(machine_diff["skill_md_changed"])
        self.assertEqual(
            machine_diff["categories"]["scripts"]["added"],
            ["scripts/new.py"],
        )

        write_skill(
            self.source,
            name="demo",
            description="drifted v3",
            body="source changed after check\n",
        )
        updated = await self.service.update("demo", candidate.candidate_hash)

        installed = self.service.skills_root / "packages" / "demo" / "SKILL.md"
        text = installed.read_text(encoding="utf-8")
        self.assertIn("candidate v2", text)
        self.assertNotIn("source changed after check", text)
        self.assertEqual(updated.description, "Demo skill v2")
        self.assertEqual(updated.content_hash, candidate.candidate_hash)
        self.assertEqual(updated.revision, 2)

    async def test_update_rejects_expired_candidate(self):
        write_skill(
            self.source,
            name="demo",
            description="Demo skill v2",
            body="candidate\n",
        )
        candidate = (await self.service.check_update("demo")).candidate
        await self.service.set_enabled("demo", False)

        with self.assertRaisesRegex(ValueError, "请重新检查更新"):
            await self.service.update("demo", candidate.candidate_hash)

    async def test_unchanged_candidate_cannot_advance_revision(self):
        candidate = (await self.service.check_update("demo")).candidate

        with self.assertRaisesRegex(ValueError, "内容相同"):
            await self.service.update("demo", candidate.candidate_hash)

        self.assertEqual((await self.service.registry.get("demo")).revision, 1)

    async def test_check_update_reports_semantic_summary_or_explicit_failure(self):
        write_skill(
            self.source,
            name="demo",
            description="Demo v2",
            body="new workflow\n",
        )
        summarizer = RecordingSummarizer()
        summarized_service = SkillApplicationService(
            self.workspace,
            registry=self.service.registry,
            diff_summarizer=summarizer,
        )

        report = await summarized_service.check_update("demo")

        self.assertEqual(
            report.semantic_summary,
            "Workflow changed; review scripts.",
        )
        self.assertIsNone(report.summary_error)
        self.assertEqual(len(summarizer.calls), 1)

        failed_service = SkillApplicationService(
            self.workspace,
            registry=self.service.registry,
            diff_summarizer=RecordingSummarizer(
                error=LLMTransientError("offline")
            ),
        )
        failed = await failed_service.check_update("demo")
        self.assertIsNone(failed.semantic_summary)
        self.assertIn("offline", failed.summary_error)
        self.assertTrue(failed.candidate.diff.changed)

    async def test_explicit_source_change_is_reported_as_replace(self):
        replacement = Path(self.temporary.name) / "replacement"
        write_skill(
            replacement,
            name="demo",
            description="Replacement",
            body="replacement workflow\n",
        )

        report = await self.service.check_update(
            "demo",
            replacement_source=SkillSourceRef(
                "local",
                str(replacement.resolve()),
            ),
        )

        self.assertEqual(report.candidate.operation, "replace")
        self.assertNotEqual(
            report.candidate.old_source,
            report.candidate.source,
        )


class SkillConsoleAdapterTest(unittest.IsolatedAsyncioTestCase):
    async def test_local_install_and_management_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = HelperMeHome(root / ".helperme")
            workspace.initialize()
            source = root / "source"
            write_skill(source, name="demo", description="Demo")
            adapter = SkillConsoleAdapter(SkillApplicationService(workspace))

            install = await adapter.execute_if_handled(
                f"/skill install {source}"
            )
            listing = await adapter.execute_if_handled("/skill list")
            test = await adapter.execute_if_handled("/skill test demo")
            enable = await adapter.execute_if_handled("/skill set_enabled demo true")

            self.assertIn("已安装并启用", install)
            self.assertIn("demo [enabled]", listing)
            self.assertIn("校验通过", test)
            self.assertIn("下一个 Step", enable)
