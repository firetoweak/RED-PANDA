import asyncio
import unittest

import httpx

from redpanda.skills.models import SkillSourceRef
from redpanda.skills.sources import SkillSourceError, SkillSourceRouter
from tests.skills.test_sources import skill_zip


class ClosingTransport(httpx.MockTransport):
    def __init__(self, handler, close_error):
        super().__init__(handler)
        self.close_error = close_error

    async def aclose(self):
        raise self.close_error


class SkillSourceLifecycleErrorsTest(unittest.IsolatedAsyncioTestCase):
    async def test_close_network_error_becomes_source_failure(self):
        router = SkillSourceRouter(transport=ClosingTransport(
            lambda request: httpx.Response(200, content=b"ok"),
            httpx.ConnectError("close offline"),
        ))
        with self.assertRaises(SkillSourceError) as caught:
            await router._download("https://example.test/skill")
        self.assertIsInstance(caught.exception.__cause__, httpx.ConnectError)

    async def test_known_request_and_close_errors_become_source_failure(self):
        def request_failure(request):
            raise httpx.ConnectError("request offline")

        router = SkillSourceRouter(transport=ClosingTransport(
            request_failure, httpx.ConnectError("close offline"),
        ))
        with self.assertRaises(SkillSourceError) as caught:
            await router._download("https://example.test/skill")
        self.assertIsInstance(caught.exception.__cause__, ExceptionGroup)

    async def test_unknown_or_cancelled_request_is_not_hidden_by_close(self):
        for error in (RuntimeError("bug"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                def request_failure(request):
                    raise error

                router = SkillSourceRouter(transport=ClosingTransport(
                    request_failure, httpx.ConnectError("close offline"),
                ))
                with self.assertRaises(BaseExceptionGroup) as caught:
                    await router._download("https://example.test/skill")
                self.assertIs(caught.exception.exceptions[0], error)

    async def test_bad_zip_crc_becomes_source_failure(self):
        content = bytearray(skill_zip())
        content[content.index(b"workflow")] ^= 1
        router = SkillSourceRouter(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=bytes(content)),
        ))
        with self.assertRaisesRegex(SkillSourceError, "ZIP 内容损坏"):
            await router.fetch(SkillSourceRef("url", "https://example.test/skill.zip"))

    async def test_unknown_close_error_passes_through(self):
        error = RuntimeError("close bug")
        router = SkillSourceRouter(transport=ClosingTransport(
            lambda request: httpx.Response(200, content=b"ok"), error,
        ))
        with self.assertRaises(RuntimeError) as caught:
            await router._download("https://example.test/skill")
        self.assertIs(caught.exception, error)
