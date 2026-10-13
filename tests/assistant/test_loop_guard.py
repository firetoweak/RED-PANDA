import unittest
from datetime import datetime, timezone

from redpanda.assistant.loop_guard import LoopGuard, SOURCE
from redpanda.assistant.loop_guard_strategies import Action, ConsecutiveActions
from redpanda.assistant.system_messages import SYSTEM_MESSAGE, SystemMessage
from redpanda.runtime import Event, InvokeTool, ModelDecision, StepCommitted, DomainFactCommitted
from redpanda.runtime.events import DeliveryIdentity
from redpanda.runtime.codec import EVENT_SCHEMA_VERSION
from redpanda.runtime.model import Command, Step


def event(sequence, names=("read",)):
    effects = tuple(InvokeTool(name, (("path", "a"),)) for name in names)
    step = Step(
        f"step-{sequence}", "trigger", 1, "basis", max(1, sequence - 1),
        ModelDecision(content="working", command_requests=effects),
        tuple(Command(f"cmd-{sequence}-{i}", effect) for i, effect in enumerate(effects)),
    )
    return Event(
        event_id=f"event-{sequence}", session_id="s", sequence=sequence,
        payload=StepCommitted(step),
        occurred_at=datetime.now(timezone.utc), causation_id=None, correlation_id=None,
        schema_version=EVENT_SCHEMA_VERSION, artifact_refs=(),
    )


def notice(sequence, cause):
    return Event(
        event_id=f"event-{sequence}", session_id="s", sequence=sequence,
        payload=DomainFactCommitted(SYSTEM_MESSAGE, SystemMessage(
            text="任务可能出现环路", severity="info", related_session_id=None).model_dump()),
        occurred_at=datetime.now(timezone.utc), causation_id=f"event-{cause}", correlation_id=None,
        schema_version=EVENT_SCHEMA_VERSION, artifact_refs=(),
        delivery=DeliveryIdentity(SOURCE, f"notice-{sequence}"),
    )


class LoopGuardTest(unittest.TestCase):
    def test_strict_fingerprint(self):
        def fp(value, tool="read"):
            return Action(1, "c", tool, value).fingerprint
        self.assertEqual(fp({"b": {"y": 2, "x": 1}, "a": [1, 2]}),
                         fp({"a": [1, 2], "b": {"x": 1, "y": 2}}))
        for left, right in [
            ({"a": [1, 2]}, {"a": [2, 1]}),
            ({"a": "a"}, {"a": "./a"}),
            ({"a": "a"}, {"a": " a"}),
            ({}, {"a": None}),
            ({"a": 1}, {"a": "1"}),
            ({"a": 1}, {"a": True}),
            ({"a": 1}, {"a": 1.0}),
        ]:
            self.assertNotEqual(fp(left), fp(right))
        self.assertNotEqual(fp({}, "read"), fp({}, "write"))

    def test_shared_coverage_comes_from_delivered_system_messages(self):
        guard = LoopGuard()
        history = tuple(event(i) for i in (2, 7, 12))
        first = guard.inspect(history, 12)
        self.assertEqual(first["evidence"][0]["new_count"], 3)
        history += (notice(13, 12), event(16), event(21))
        self.assertIsNone(guard.inspect(history, 21))
        history += (event(26),)
        second = guard.inspect(history, 26)
        self.assertEqual(second["covered_after"], 12)
        self.assertEqual(second["evidence"][0]["new_count"], 3)
        self.assertEqual(guard.inspect(history, 12), first)
        history += (notice(27, 26),)
        self.assertIsNone(LoopGuard((ConsecutiveActions(2),)).inspect(history, 27))
        self.assertEqual(LoopGuard().inspect(history[:3], 12), first)

    def test_parallel_actions_are_distinct_but_alternation_is_not_continuous(self):
        result = LoopGuard().inspect((event(2, ("read", "read", "read")),), 2)
        self.assertEqual(result["evidence"][0]["new_count"], 3)
        self.assertEqual(len({a["command_id"] for a in result["evidence"][0]["references"]}), 3)
        self.assertIsNone(LoopGuard().inspect((event(2, ("a", "b", "a", "b", "a", "b")),), 2))

    def test_multiple_strategies_share_one_observation(self):
        result = LoopGuard((ConsecutiveActions(2), ConsecutiveActions(3))).inspect(
            (event(2, ("read", "read", "read")),), 2)
        self.assertEqual(len(result["evidence"]), 2)
