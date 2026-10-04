import json
import tempfile
import unittest
from pathlib import Path

from redpanda.cli.models import CliHealth, CliRecord, CliSourceRef, utc_now
from redpanda.cli.registry import CliRegistry


def make_record(name: str, *, version: str | None = "1.0.0") -> CliRecord:
    return CliRecord(
        name=name,
        description=f"{name} CLI",
        source=CliSourceRef("manifest", name),
        version=version,
        resolved_path=f"/usr/bin/{name}",
        health=CliHealth(
            help_ok=True,
            version_ok=True,
            help_mentions_json=False,
            checked_at=utc_now(),
        ),
    )


class CliRegistryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.registry = CliRegistry(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    async def test_missing_file_reads_as_empty(self):
        self.assertEqual(await self.registry.list_clis(), ())

    async def test_add_get_replace_remove_roundtrip(self):
        await self.registry.add(make_record("rg"))

        stored = await self.registry.get("rg")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.version, "1.0.0")
        self.assertEqual(stored.revision, 1)

        replaced = await self.registry.replace(make_record("rg", version="2.0.0"))
        self.assertEqual(replaced.version, "2.0.0")
        self.assertEqual(replaced.revision, 2)

        removed = await self.registry.remove("rg")
        self.assertEqual(removed.name, "rg")
        self.assertIsNone(await self.registry.get("rg"))

    async def test_add_rejects_duplicate_name(self):
        await self.registry.add(make_record("rg"))
        with self.assertRaises(ValueError):
            await self.registry.add(make_record("rg"))

    async def test_replace_and_remove_require_existing(self):
        with self.assertRaises(KeyError):
            await self.registry.replace(make_record("rg"))
        with self.assertRaises(KeyError):
            await self.registry.remove("rg")

    async def test_persists_across_instances(self):
        await self.registry.add(make_record("rg"))
        reloaded = CliRegistry(self.root)
        record = await reloaded.get("rg")
        self.assertIsNotNone(record)
        self.assertEqual(record.source.locator, "rg")

    async def test_envelope_requires_exact_keys_and_version(self):
        self._write_payload({"version": 2, "clis": []})
        with self.assertRaises(ValueError):
            await self.registry.list_clis()

        self._write_payload({"clis": []})
        with self.assertRaises(ValueError):
            await self.registry.list_clis()

    async def test_duplicate_names_are_rejected(self):
        record = make_record("rg").to_dict()
        self._write_payload({"version": 1, "clis": [record, record]})
        with self.assertRaises(ValueError):
            await self.registry.list_clis()

    def _write_payload(self, payload: dict) -> None:
        (self.root / "registry.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
