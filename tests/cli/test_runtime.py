import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from redpanda.cli.models import CliHealth, CliRecord, CliSourceRef, utc_now
from redpanda.cli.registry import CliRegistry
from redpanda.cli.runtime import (
    CLI_DESCRIPTION_OMITTED,
    LOAD_CLI,
    CliToolCatalog,
    LoadCliInput,
)


def make_record(name: str) -> CliRecord:
    return CliRecord(
        name=name,
        description=f"{name} CLI",
        source=CliSourceRef("manifest", name),
        version="1.0.0",
        resolved_path=f"/usr/bin/{name}",
        health=CliHealth(
            help_ok=True,
            version_ok=True,
            help_mentions_json=True,
            checked_at=utc_now(),
        ),
    )


class CliRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.registry = CliRegistry(Path(self.temporary.name))
        await self.registry.add(make_record("rg"))
        self.catalog = CliToolCatalog(self.registry)

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_load_cli_is_one_plain_tool_spec_with_catalog(self):
        specs = {spec.name: spec for spec in self.catalog.tool_specs()}

        self.assertEqual(set(specs), {LOAD_CLI})
        # 目录通过上下文事实（catalog 同步）提供，不拼进工具描述。
        self.assertNotIn("- rg: rg CLI", specs[LOAD_CLI].description)
        self.assertTrue(specs[LOAD_CLI].exclusive_batch)

        result = await specs[LOAD_CLI].handler(LoadCliInput(cli_id="rg"))

        self.assertEqual(result["code"], "CLI_LOADED")
        self.assertEqual(result["data"]["cli_id"], "rg")
        self.assertEqual(result["data"]["version"], "1.0.0")
        self.assertEqual(result["data"]["resolved_path"], "/usr/bin/rg")
        self.assertEqual(result["data"]["source"]["kind"], "manifest")
        self.assertTrue(result["data"]["health"]["help_mentions_json"])

    async def test_load_cli_rejects_unknown_id(self):
        specs = {spec.name: spec for spec in self.catalog.tool_specs()}

        result = await specs[LOAD_CLI].handler(LoadCliInput(cli_id="fd"))

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "CLI_NOT_FOUND")

    async def test_specs_refresh_catalog_but_old_closure_rejects_change(self):
        old_specs = {spec.name: spec for spec in self.catalog.tool_specs()}
        await self.registry.replace(make_record("rg"))

        stale = await old_specs[LOAD_CLI].handler(LoadCliInput(cli_id="rg"))

        self.assertEqual(stale["code"], "CLI_CATALOG_STALE")

        new_specs = {spec.name: spec for spec in self.catalog.tool_specs()}
        loaded = await new_specs[LOAD_CLI].handler(LoadCliInput(cli_id="rg"))
        self.assertEqual(loaded["code"], "CLI_LOADED")

    async def test_catalog_over_budget_shortens_description_but_cli_remains_loadable(self):
        await self.registry.replace(replace(make_record("rg"), description="x" * 1000))
        catalog = CliToolCatalog(self.registry, max_catalog_chars=80)
        self.assertEqual(catalog.catalog_records()[0].description, CLI_DESCRIPTION_OMITTED)
        specs = catalog.tool_specs()
        self.assertEqual([spec.to_openai_tool() for spec in specs],
                         [spec.to_openai_tool() for spec in self.catalog.tool_specs()])
        loaded = await specs[0].handler(LoadCliInput(cli_id="rg"))
        self.assertEqual(loaded["code"], "CLI_LOADED")
        self.assertEqual(loaded["data"]["description"], "x" * 1000)

    async def test_forty_clis_stay_visible_even_when_minimum_catalog_exceeds_budget(self):
        names = {f"cli-{index:02}" for index in range(40)}
        for name in sorted(names):
            await self.registry.add(replace(make_record(name), description="x" * 500))
        original = self.registry.snapshot()
        for budget in (4000, 4):
            with self.subTest(budget=budget):
                catalog = CliToolCatalog(self.registry, max_catalog_chars=budget)
                records = catalog.catalog_records()
                self.assertEqual({record.name for record in records}, names | {"rg"})
                rendered = "\n".join(f"- {record.name}: {record.description}" for record in records)
                if budget == 4000:
                    self.assertLessEqual(len(rendered), budget)
                else:
                    self.assertGreater(len(rendered), budget)
                self.assertEqual(next(record.description for record in records if record.name == "cli-39"),
                                 CLI_DESCRIPTION_OMITTED)
                loaded = await catalog.tool_specs()[0].handler(LoadCliInput(cli_id="cli-39"))
                self.assertEqual(loaded["code"], "CLI_LOADED")
                self.assertEqual(loaded["data"]["description"], "x" * 500)
                self.assertEqual(self.registry.snapshot(), original)
