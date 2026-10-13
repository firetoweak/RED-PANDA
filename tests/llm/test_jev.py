import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import httpx
from pydantic import ValidationError

from redpanda.llm.jev import JevClient, JevSettings


def settings(**changes):
    values = dict(enabled=True, api_key="test-key", model="jev-1.13.0",
                  interval_steps=1, recent_steps=8, state_max_tokens=2048,
                  timeout_seconds=0.1, remind_confidence=0.8, pause_confidence=0.95)
    return JevSettings(**(values | changes))


def response(choice="pause", confidence=0.99):
    probabilities = {"continue": 0.0, "remind": 0.0, "pause": 0.0}
    probabilities[choice] = 1.0
    return {"model": "jev-1.13.0", "answers": {"intervention": {
        "type": "choice", "choice": choice, "confidence": confidence,
        "probabilities": probabilities,
    }}, "usage": {"input_tokens": 100, "output_tokens": 20}}


class JevBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def test_request_uses_pinned_model_and_choice_without_generated_text(self):
        captured = []

        def handle(request):
            captured.append(json.loads(request.content))
            self.assertEqual(request.headers["Authorization"], "Bearer test-key")
            return httpx.Response(200, json=response())

        client = JevClient(Path("unused"), transport=httpx.MockTransport(handle))
        question = {"type": "choice", "instructions": "Choose.", "criteria": {"pause": "Stop."}}
        result = await client.evaluate("中文状态", question, settings())
        self.assertEqual(result.choice, "pause")
        self.assertEqual(captured, [{"model": "jev-1.13.0", "state": "中文状态",
                                     "questions": {"intervention": question}}])

    async def test_service_protocol_and_low_confidence_failures_are_unavailable(self):
        cases = [httpx.Response(401), httpx.Response(529), httpx.Response(200, text="invalid"),
                 httpx.Response(200, json={}),
                 httpx.Response(200, json=response(confidence=0.9)),
                 httpx.Response(200, json=response() | {"model": "jev-other"})]
        for reply in cases:
            with self.subTest(reply=reply.status_code):
                client = JevClient(Path("unused"), transport=httpx.MockTransport(lambda _: reply))
                self.assertIsNone(await client.evaluate("state", {}, settings()))

    async def test_transport_failure_is_unavailable_but_internal_error_escapes(self):
        for error in (httpx.ConnectError("offline"), RuntimeError("broken adapter")):
            def handle(_):
                raise error
            client = JevClient(Path("unused"), transport=httpx.MockTransport(handle))
            if isinstance(error, httpx.TransportError):
                self.assertIsNone(await client.evaluate("state", {}, settings()))
            else:
                with self.assertRaisesRegex(RuntimeError, "broken adapter"):
                    await client.evaluate("state", {}, settings())

    def test_optional_settings_do_not_hide_corrupt_configuration(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "jev.json"
            client = JevClient(path)
            self.assertIsNone(client.settings())
            for config in (settings(enabled=False), settings(api_key="")):
                path.write_text(config.model_dump_json(), encoding="utf-8")
                self.assertIsNone(client.settings())
            path.write_text('{"enabled":true}', encoding="utf-8")
            with self.assertRaises(ValidationError):
                client.settings()

    def test_floating_alias_is_not_a_calibrated_model_version(self):
        with self.assertRaises(ValidationError):
            settings(model="jev-latest")
