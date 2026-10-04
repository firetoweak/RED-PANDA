import tempfile
import unittest
from pathlib import Path

from redpanda.cli.models import CliHealth, CliRecord, CliSourceRef, utc_now
from redpanda.cli.registry import CliRegistry
from redpanda.cli.runtime import LOAD_CLI, CliToolCatalog, LoadCliInput


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
        self.assertEqual(result["data"]["revision"], 1)
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
        self.assertEqual(loaded["data"]["revision"], 2)

    async def test_catalog_over_budget_fails_loudly(self):
        catalog = CliToolCatalog(self.registry, max_catalog_chars=4)
        with self.assertRaises(RuntimeError):
            catalog.tool_specs()
