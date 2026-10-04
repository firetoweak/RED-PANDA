import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock
import zipfile

import httpx

from redpanda.skills.models import SkillSourceRef
from redpanda.skills.approval import (
    SkillInstallProposalInput,
    create_skill_install_proposal_spec,
)
from redpanda.skills.sources import SkillSourceError, SkillSourceRouter
from redpanda.skills.package import SkillPackageError
from redpanda.tools.control import ControlPreparationFailure
from tests.skills.test_package import write_skill


def skill_zip(
    *,
    root: str = "repository-commit",
    body: str = "workflow",
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            f"{root}/SKILL.md",
            "---\nname: remote-skill\n"
            "description: Remote workflow\n---\n"
            f"{body}\n",
        )
        archive.writestr(f"{root}/references/guide.md", "guide")
    return buffer.getvalue()


class SkillSourceRouterTest(unittest.IsolatedAsyncioTestCase):
    async def test_package_errors_preserve_reason_for_every_source(self):
        markdown = b"---\nname: demo\ndescription: ''\n---\nbody\n"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("repo-commit/SKILL.md", markdown)

        async def handler(request):
            if request.url.host == "api.github.com":
                return httpx.Response(200, json={"sha": "abc123"})
            if request.url.host == "codeload.github.com":
                return httpx.Response(200, content=buffer.getvalue())
            return httpx.Response(200, content=markdown)

        router = SkillSourceRouter(transport=httpx.MockTransport(handler))
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "SKILL.md").write_bytes(markdown)
            for source in (
                SkillSourceRef("local", directory),
                SkillSourceRef("url", "https://example.test/SKILL.md"),
                SkillSourceRef("github", "owner/repository"),
            ):
                with self.subTest(kind=source.kind):
                    with self.assertRaisesRegex(
                        SkillSourceError, "package 格式无效: Skill description 不能为空"
                    ) as captured:
                        await router.fetch(source)
                    self.assertIsInstance(captured.exception.__cause__, SkillPackageError)

    async def test_local_read_error_is_distinct_from_package_error(self):
        reader = Mock()
        reader.read.side_effect = PermissionError("access denied")

        with self.assertRaisesRegex(
            SkillSourceError, "无法读取本地 Skill source: .*access denied"
        ) as captured:
            await SkillSourceRouter(reader).fetch(SkillSourceRef("local", "skill"))

        self.assertIsInstance(captured.exception.__cause__, PermissionError)

    async def test_connect_error_becomes_external_skill_source_error(self):
        async def handler(request):
            raise httpx.ConnectError("offline", request=request)

        router = SkillSourceRouter(transport=httpx.MockTransport(handler))

        with self.assertRaisesRegex(
            SkillSourceError,
            "ConnectError",
        ) as captured:
            await router.fetch(SkillSourceRef(
                "url",
                "https://example.test/SKILL.md",
            ))

        self.assertIsInstance(captured.exception.__cause__, httpx.ConnectError)

    async def test_local_source_keeps_explicit_source_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "skill"
            write_skill(source, name="demo")
            source_ref = SkillSourceRef("local", str(source), "ignored-ref")

            bundle = await SkillSourceRouter().fetch(source_ref)

            self.assertEqual(bundle.source, source_ref)
            self.assertTrue(bundle.resolved_ref.startswith("file:"))

    async def test_direct_url_accepts_single_markdown_or_zip(self):
        markdown = (
            b"---\nname: direct\ndescription: Direct workflow\n---\nbody\n"
        )

        async def handler(request):
            if request.url.path.endswith("skill.md"):
                return httpx.Response(200, content=markdown)
            return httpx.Response(200, content=skill_zip())

        router = SkillSourceRouter(
            transport=httpx.MockTransport(handler)
        )
        direct = await router.fetch(SkillSourceRef(
            "url", "https://example.test/skill.md"
        ))
        zipped = await router.fetch(SkillSourceRef(
            "url", "https://example.test/skill.zip"
        ))

        self.assertEqual(direct.name, "direct")
        self.assertEqual(zipped.name, "remote-skill")
        self.assertEqual(
            [item.relative_path for item in zipped.files],
            ["SKILL.md", "references/guide.md"],
        )

    async def test_github_resolves_commit_before_fetching_archive(self):
        requests = []

        async def handler(request):
            requests.append(str(request.url))
            if request.url.host == "api.github.com":
                return httpx.Response(
                    200,
                    content=json.dumps({"sha": "abc123"}).encode(),
                )
            return httpx.Response(200, content=skill_zip())

        source = SkillSourceRef("github", "owner/repository", "main")
        bundle = await SkillSourceRouter(
            transport=httpx.MockTransport(handler)
        ).fetch(source)

        self.assertEqual(bundle.source, source)
        self.assertEqual(
            bundle.resolved_ref,
            "https://github.com/owner/repository/tree/abc123",
        )
        self.assertIn("/commits/main", requests[0])
        self.assertIn("/zip/abc123", requests[1])

    async def test_github_tree_url_selects_one_skill_from_monorepo(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                "repo-commit/skills/pdf/SKILL.md",
                "---\nname: pdf\ndescription: PDF workflow\n---\npdf body\n",
            )
            archive.writestr(
                "repo-commit/skills/doc/SKILL.md",
                "---\nname: doc\ndescription: DOC workflow\n---\ndoc body\n",
            )

        async def handler(request):
            if request.url.host == "api.github.com":
                return httpx.Response(200, json={"sha": "abc123"})
            return httpx.Response(200, content=buffer.getvalue())

        source = SkillSourceRef(
            "github",
            "https://github.com/owner/repository/tree/main/skills/pdf",
        )
        bundle = await SkillSourceRouter(
            transport=httpx.MockTransport(handler)
        ).fetch(source)

        self.assertEqual(bundle.name, "pdf")
        self.assertEqual(
            bundle.resolved_ref,
            "https://github.com/owner/repository/tree/abc123/skills/pdf",
        )

    async def test_rejects_insecure_url_archive_traversal_and_multiple_skills(self):
        with self.assertRaisesRegex(SkillSourceError, "HTTPS"):
            await SkillSourceRouter().fetch(SkillSourceRef(
                "url", "http://example.test/skill.md"
            ))

        for entries in (
            {
                "root/SKILL.md": "---\nname: demo\ndescription: demo\n---\n",
                "../outside": "bad",
            },
            {
                "one/SKILL.md": "---\nname: one\ndescription: one\n---\n",
                "two/SKILL.md": "---\nname: two\ndescription: two\n---\n",
            },
        ):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                for path, content in entries.items():
                    archive.writestr(path, content)

            async def handler(_request, payload=buffer.getvalue()):
                return httpx.Response(200, content=payload)

            router = SkillSourceRouter(
                transport=httpx.MockTransport(handler)
            )
            with self.assertRaises(SkillSourceError):
                await router.fetch(SkillSourceRef(
                    "url", "https://example.test/skill.zip"
                ))


class SkillInstallProposalBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def test_expected_source_error_is_a_recoverable_tool_result(self):
        service = Mock()
        service.prepare_install = AsyncMock(
            side_effect=SkillSourceError("source offline")
        )
        spec = create_skill_install_proposal_spec(service)

        result = await spec.handler(SkillInstallProposalInput(
            source_kind="url",
            locator="https://example.test/SKILL.md",
        ))

        self.assertIsInstance(result, ControlPreparationFailure)
        self.assertFalse(result.result["ok"])
        self.assertEqual(result.result["code"], "SKILL_SOURCE_ERROR")
        self.assertIn("source offline", result.result["error"])

    async def test_internal_error_is_not_converted(self):
        service = Mock()
        service.prepare_install = AsyncMock(
            side_effect=RuntimeError("internal bug")
        )
        spec = create_skill_install_proposal_spec(service)

        with self.assertRaisesRegex(RuntimeError, "internal bug"):
            await spec.handler(SkillInstallProposalInput(
                source_kind="url",
                locator="https://example.test/SKILL.md",
            ))
