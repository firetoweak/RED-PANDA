from __future__ import annotations

import unittest

from tests.benchmarks.context_cache_live import build_variants


def manifest(position: int, content: str) -> dict[str, object]:
    return {
        "schema": "decision-replay-manifest/v2",
        "decision_basis": {
            "observed_journal_position": position,
            "decision_cursor": position,
        },
        "request": {
            "messages": [
                {
                    "role": "tool",
                    "tool_call_id": "call-1",
                    "content": content,
                }
            ],
            "tools": None,
        },
    }


class ContextCacheVariantTest(unittest.TestCase):
    def test_no_age_restores_first_visible_tool_result(self):
        full = "result:" + "x" * 1_000
        stub = '{"externalized":true}'

        variants = build_variants(
            [manifest(1, full), manifest(2, stub)],
            batch_clear_tokens=1,
        )

        self.assertEqual(
            variants["no_age"][1].messages[0]["content"],
            full,
        )
        self.assertEqual(
            variants["sliding"][1].messages[0]["content"],
            stub,
        )
        self.assertEqual(
            variants["batch"][1].messages[0]["content"],
            stub,
        )

    def test_batch_waits_until_clear_threshold_is_reached(self):
        full = "result:" + "x" * 1_000
        stub = '{"externalized":true}'

        variants = build_variants(
            [manifest(1, full), manifest(2, stub)],
            batch_clear_tokens=1_000_000,
        )

        self.assertEqual(
            variants["batch"][1].messages[0]["content"],
            full,
        )
