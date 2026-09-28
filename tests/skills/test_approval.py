import shutil
import tempfile
import unittest
from pathlib import Path

from helperme.tools.control import ControlApprovalProposal
from helperme.paths import HelperMeHome
from helperme.skills.application import SkillApplicationService
from helperme.skills.approval import (
    SkillSetEnabledApprovalHandler,
    SkillSetEnabledProposalInput,
    SkillInstallApprovalHandler,
    SkillInstallProposalInput,
    SkillUninstallApprovalHandler,
    SkillUninstallProposalInput,
    create_skill_uninstall_proposal_spec,
    SkillUpdateApprovalHandler,
    SkillUpdateProposalInput,
    create_skill_set_enabled_proposal_spec,
    create_skill_install_proposal_spec,
    create_skill_update_proposal_spec,
)
from tests.skills.test_package import write_skill


class SkillInstallApprovalTest(unittest.IsolatedAsyncioTestCase):
    async def test_update_proposal_and_approval_use_frozen_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = HelperMeHome(root / ".helperme")
            workspace.initialize()
            source = root / "source"
            write_skill(source, name="demo", body="v1\n")
            service = SkillApplicationService(workspace)
            await service.install_local(source)
            write_skill(source, name="demo", body="v2\n")

            request = await create_skill_update_proposal_spec(service).handler(
                SkillUpdateProposalInput(skill_id="demo")
            )
            execution = await SkillUpdateApprovalHandler(service).execute(
                request.payload
            )

            self.assertTrue(execution.succeeded)
            installed = workspace.skills_root / "packages" / "demo" / "SKILL.md"
            self.assertIn("v2", installed.read_text(encoding="utf-8"))

    async def test_already_installed_is_a_deterministic_proposal_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = HelperMeHome(root / ".helperme")
            workspace.initialize()
            source = root / "source"
            write_skill(source, name="demo")
            service = SkillApplicationService(workspace)
            await service.install_local(source)

            result = await create_skill_install_proposal_spec(service).handler(
                SkillInstallProposalInput(
                    source_kind="local",
                    locator=str(source),
                )
            )

            self.assertEqual(result["code"], "SKILL_ALREADY_INSTALLED")

    async def test_same_content_from_new_source_keeps_current_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = HelperMeHome(root / ".helperme")
            workspace.initialize()
            first_source = root / "first-source"
            current_source = root / "current-source"
            write_skill(first_source, name="demo", body="same body\n")
            write_skill(current_source, name="demo", body="same body\n")
            service = SkillApplicationService(workspace)
            spec = create_skill_install_proposal_spec(service)

            first = await spec.handler(SkillInstallProposalInput(
                source_kind="local",
                locator=str(first_source),
            ))
            current = await spec.handler(SkillInstallProposalInput(
                source_kind="local",
                locator=str(current_source),
            ))

            self.assertIsInstance(first, ControlApprovalProposal)
            self.assertIsInstance(current, ControlApprovalProposal)
            self.assertEqual(
                first.payload["content_hash"],
                current.payload["content_hash"],
            )
            self.assertEqual(
                current.payload["source"]["locator"],
                str(current_source),
            )

            await SkillInstallApprovalHandler(service).execute(current.payload)

            record = await service.registry.get("demo")
            self.assertEqual(record.source.locator, str(current_source))

    async def test_proposal_freezes_candidate_and_approval_installs_exact_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = HelperMeHome(root / ".helperme")
            workspace.initialize()
            source = root / "source"
            write_skill(
                source,
                name="demo",
                description="Frozen v1",
                body="frozen body\n",
            )
            service = SkillApplicationService(workspace)
            spec = create_skill_install_proposal_spec(service)

            request = await spec.handler(SkillInstallProposalInput(
                source_kind="local",
                locator=str(source),
            ))
            self.assertIsInstance(request, ControlApprovalProposal)
            frozen_hash = request.payload["content_hash"]

            write_skill(
                source,
                name="demo",
                description="Drifted v2",
                body="drifted body\n",
            )
            execution = await SkillInstallApprovalHandler(service).execute(
                request.payload
            )

            record = await service.registry.get("demo")
            installed = service.skills_root / "packages" / "demo" / "SKILL.md"
            text = installed.read_text(encoding="utf-8")
            self.assertTrue(execution.succeeded)
            self.assertTrue(record.enabled)
            self.assertEqual(record.revision, 1)
            self.assertEqual(record.content_hash, frozen_hash)
            self.assertIn("Frozen v1", text)
            self.assertNotIn("Drifted v2", text)
            shutil.rmtree(source)
            from helperme.skills.runtime import LoadSkillInput
            load = next(item for item in service.tool_catalog.tool_specs() if item.name == "load_skill")
            loaded = await load.handler(LoadSkillInput(skill_id="demo"))
            self.assertTrue(loaded["ok"])
            self.assertIn("frozen body", loaded["data"]["content"])

    async def test_missing_frozen_install_candidate_is_known_execution_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = HelperMeHome(root / ".helperme")
            workspace.initialize()
            source = root / "source"
            write_skill(source, name="demo")
            service = SkillApplicationService(workspace)

            request = await create_skill_install_proposal_spec(service).handler(
                SkillInstallProposalInput(
                    source_kind="local",
                    locator=str(source),
                )
            )
            self.assertIsInstance(request, ControlApprovalProposal)
            candidate = (
                service.install_candidates.root
                / request.payload["content_hash"]
            )
            shutil.rmtree(candidate)

            execution = await SkillInstallApprovalHandler(service).execute(
                request.payload
            )

            self.assertFalse(execution.succeeded)
            self.assertIn("candidate 不存在", execution.message)
            self.assertIsNone(await service.registry.get("demo"))

    async def test_install_proposal_is_both_approval_boundary_and_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = HelperMeHome(Path(directory) / ".helperme")
            service = SkillApplicationService(workspace)
            spec = create_skill_install_proposal_spec(service)

            self.assertTrue(spec.control_boundary)
            self.assertTrue(spec.exclusive_batch)

    async def test_set_enabled_proposal_freezes_revision_and_rejects_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = HelperMeHome(root / ".helperme")
            workspace.initialize()
            source = root / "source"
            write_skill(source, name="demo")
            service = SkillApplicationService(workspace)
            await service.install_local(source)
            spec = create_skill_set_enabled_proposal_spec(service)
            request = await spec.handler(SkillSetEnabledProposalInput(skill_id="demo", enabled=False))

            await service.set_enabled("demo", True)
            await service.set_enabled("demo", False)

            result = await SkillSetEnabledApprovalHandler(service).execute(request.payload)
            self.assertFalse(result.succeeded)
            self.assertIn("请重新提交", result.message)

            self.assertTrue(spec.control_boundary)
            self.assertTrue(spec.exclusive_batch)

    async def test_uninstall_rejects_changed_revision_then_removes_current_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            write_skill(source, name="demo")
            service = SkillApplicationService(HelperMeHome(root / "home"))
            await service.install_local(source)
            spec = create_skill_uninstall_proposal_spec(service)
            request = await spec.handler(SkillUninstallProposalInput(skill_id="demo"))
            await service.set_enabled("demo", False)
            handler = SkillUninstallApprovalHandler(service)
            stale = await handler.execute(request.payload)
            self.assertFalse(stale.succeeded)
            self.assertTrue((service.skills_root / "packages/demo").is_dir())
            current = await spec.handler(SkillUninstallProposalInput(skill_id="demo"))
            removed = await handler.execute(current.payload)
            self.assertTrue(removed.succeeded)
            self.assertIsNone(await service.registry.get("demo"))
            self.assertFalse((service.skills_root / "packages/demo").exists())
            self.assertTrue(source.is_dir())

    async def test_set_enabled_can_disable_without_reading_a_damaged_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            write_skill(source, name="demo")
            service = SkillApplicationService(HelperMeHome(root / "home"))
            await service.install_local(source)
            (service.skills_root / "packages/demo/SKILL.md").write_text("damaged", encoding="utf-8")
            request = await create_skill_set_enabled_proposal_spec(service).handler(
                SkillSetEnabledProposalInput(skill_id="demo", enabled=False)
            )
            result = await SkillSetEnabledApprovalHandler(service).execute(request.payload)
            self.assertTrue(result.succeeded)
            self.assertFalse((await service.registry.get("demo")).enabled)

    async def test_install_rechecks_catalog_budget_before_any_install_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            write_skill(source, name="demo", description="Demo")
            home = HelperMeHome(root / "home")
            service = SkillApplicationService(home)
            request = await create_skill_install_proposal_spec(service).handler(
                SkillInstallProposalInput(source_kind="local", locator=str(source))
            )
            constrained = SkillApplicationService(home, max_catalog_chars=1)
            result = await SkillInstallApprovalHandler(constrained).execute(request.payload)
            self.assertFalse(result.succeeded)
            self.assertIn("SKILL_CATALOG_LIMIT", result.message)
            self.assertEqual(await constrained.list_skills(), ())
            self.assertFalse((home.skills_root / "packages/demo").exists())

    async def test_uninstall_can_remove_registration_when_package_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            write_skill(source, name="demo")
            service = SkillApplicationService(HelperMeHome(root / "home"))
            await service.install_local(source)
            shutil.rmtree(service.skills_root / "packages/demo")
            request = await create_skill_uninstall_proposal_spec(service).handler(
                SkillUninstallProposalInput(skill_id="demo")
            )
            result = await SkillUninstallApprovalHandler(service).execute(request.payload)
            self.assertTrue(result.succeeded)
            self.assertIsNone(await service.registry.get("demo"))
