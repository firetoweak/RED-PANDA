import json
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from helperme.assistant.assembly import build_assistant_assembly
from helperme.assistant.context.projection import project_chat_messages
from helperme.assistant.compact.core import CompactBoundary, save_document
from helperme.assistant.loop_guard import LoopGuard, NOTICE, committed_notice
from helperme.assistant.loop_guard_strategies import Action, ConsecutiveActions
from helperme.config import AssistantConfig
from helperme.llm.types import LLMCallResult, LLMResponse, LLMUsage, ToolCall
from helperme.paths import HelperMeHome
from helperme.runtime import (
    Event,
    InvokeTool,
    ModelDecision,
    SqliteJournal,
    StateProjector,
    StepCommitted,
)
from helperme.runtime.json_values import thaw_value
from helperme.runtime.codec import EVENT_SCHEMA_VERSION
from helperme.runtime.model import Command, Step
from tests.fixtures.workspaces import workspace_record
from tests.session_scheduler import settle_session


def event(sequence, names=("read",), notice=None):
    effects = tuple(InvokeTool(name, (("path", "a"),)) for name in names)
    step = Step(
        f"step-{sequence}", "trigger", 1, "basis", max(1, sequence - 1),
        ModelDecision(content="working", command_requests=effects),
        tuple(Command(f"cmd-{sequence}-{i}", effect) for i, effect in enumerate(effects)),
    )
    return Event(
        event_id=f"event-{sequence}", session_id="s", sequence=sequence,
        payload=StepCommitted(step, None if notice is None else {NOTICE: notice}),
        occurred_at=datetime.now(timezone.utc), causation_id=None, correlation_id=None,
        schema_version=EVENT_SCHEMA_VERSION, artifact_refs=(),
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

    def test_overlap_requires_three_fresh_actions_and_shared_coverage(self):
        guard = LoopGuard()
        history = tuple(event(i) for i in (2, 7, 12))
        first = guard.inspect(history, 15)
        self.assertEqual(first["covered_through"], 15)
        self.assertEqual(first["evidence"][0]["new_count"], 3)
        history += (event(16, notice=first), event(21))
        self.assertIsNone(guard.inspect(history, 21))
        history += (event(26),)
        second = guard.inspect(history, 26)
        self.assertEqual(second["covered_after"], 15)
        self.assertEqual(second["evidence"][0]["new_count"], 3)
        self.assertEqual(guard.inspect(history, 15), first)
        history += (event(27, names=(), notice=second),)
        # A newly installed strategy shares the already committed coverage.
        self.assertIsNone(LoopGuard((ConsecutiveActions(2),)).inspect(history, 27))
        # A rebuilt earlier branch does not inherit the later notice.
        self.assertEqual(LoopGuard().inspect(history[:3], 15), first)

    def test_parallel_actions_are_distinct_but_alternation_is_not_continuous(self):
        notice = LoopGuard().inspect((event(2, ("read", "read", "read")),), 2)
        self.assertEqual(notice["evidence"][0]["new_count"], 3)
        self.assertEqual(len({a["command_id"] for a in notice["evidence"][0]["references"]}), 3)
        self.assertIsNone(LoopGuard().inspect((event(2, ("a", "b", "a", "b", "a", "b")),), 2))

    def test_multiple_hits_merge_and_old_text_is_replayed_verbatim(self):
        events = (event(2, ("read", "read", "read")),)
        notice = LoopGuard((ConsecutiveActions(2), ConsecutiveActions(3))).inspect(events, 2)
        self.assertEqual(len(notice["evidence"]), 2)
        notice["text"] = "<loop_guard_notice>historical wording</loop_guard_notice>"
        events += (event(3, names=(), notice=notice),)
        messages = project_chat_messages(
            events, StateProjector().project_visible("s", events), "sys"
        )
        self.assertEqual(messages[-2]["content"], notice["text"])


class RepeatingLlm:
    def __init__(self):
        self.requests = []
        self.fail_next = False

    async def chat(self, messages, model, *, tools=None):
        self.requests.append(deepcopy(messages))
        if self.fail_next:
            raise RuntimeError("provider broke")
        calls = () if len(self.requests) >= 8 else (
            ToolCall("read", "read_file", '{"path":"a.txt"}'),
        )
        return LLMCallResult(LLMResponse(content="working", calls=calls), LLMUsage(1, 1))


class LoopGuardIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_decision_commit_resume_and_prefix(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.txt").write_text("same evidence", encoding="utf-8")
            llm = RepeatingLlm()
            journal = SqliteJournal(root / "journal.sqlite")
            assembly = await build_assistant_assembly(
                AssistantConfig("test", 200000, llm),
                lambda *args: None, journal, session_id="s",
                workspace=workspace_record(root),
                home=HelperMeHome(root / "home"),
            )
            try:
                await assembly.runtime.receive_user_message("s", "read", delivery_id="u")
                await settle_session(assembly.runtime, "s", control=assembly.control)
                events = await SqliteJournal(root / "journal.sqlite").snapshot("s")
                steps = [e for e in events if isinstance(e.payload, StepCommitted)]
                notices = [(i, committed_notice(e.payload)) for i, e in enumerate(steps)
                           if committed_notice(e.payload) is not None]
                self.assertEqual([i for i, _ in notices], [3, 6])
                self.assertEqual(len(llm.requests), 8)  # reminders never stop tool calls
                first = thaw_value(notices[0][1])
                self.assertEqual(llm.requests[3][-1]["content"], first["text"])
                self.assertEqual(llm.requests[4][:len(llm.requests[3])], llm.requests[3])
                self.assertEqual(sum(m.get("content") == first["text"] for m in llm.requests[4]), 1)
                self.assertIsNone(LoopGuard().inspect(events, events[-1].sequence))
                prefix = tuple(e for e in events if e.sequence <= first["covered_through"])
                self.assertEqual(LoopGuard().inspect(prefix, first["covered_through"]), first)
                # covered_through 是决策观测位置，可决策不等于已静默，它可能落在一个 Step
                # 的两条命令结果之间。CompactBoundary 只受理命令全部终局的边界，所以往后
                # 延到最近一个静默点；inspect 自己按 position 过滤，延长不影响上面的断言。
                while assembly.runtime.projector.project("s", prefix).state.waiting_command_ids:
                    prefix = events[: len(prefix) + 1]

                # Re-run the fourth decision against the historical frozen frame.
                maker = assembly.runtime.step_runner._decision_maker
                frame = assembly.runtime.projector.project("s", prefix).next_decision
                llm.fail_next = True
                with patch.object(journal, "snapshot", AsyncMock(return_value=prefix)):
                    with self.assertRaisesRegex(RuntimeError, "provider broke"):
                        await maker.decide(frame)
                    self.assertEqual(llm.requests[-1][-1]["content"], first["text"])
                self.assertEqual(LoopGuard().inspect(prefix, first["covered_through"]), first)
                llm.fail_next = False
                with (
                    patch.object(journal, "snapshot", AsyncMock(return_value=prefix)),
                    patch.object(journal, "commit_step", AsyncMock(side_effect=RuntimeError("commit broke"))),
                ):
                    with self.assertRaisesRegex(RuntimeError, "commit broke"):
                        await assembly.runtime.step_runner.commit(frame, object())
                self.assertEqual(await journal.snapshot("s"), events)
                self.assertEqual(LoopGuard().inspect(prefix, first["covered_through"]), first)

                # A window rollover hides old messages, but never hides them from the guard.
                material = save_document(maker._projector.gateway, "s", {
                    "messages": [{"role": "user", "content": "handoff"}],
                })
                boundary = CompactBoundary(assembly.runtime, maker, maker._compact, None, None, None)
                await boundary.publish({
                    "handoff": {"artifact": material, "request": material},
                    "window": {
                        "id": "window-1", "parent": None, "upto": events[-1].sequence,
                        "cutover": events[-1].sequence, "context": material,
                        "bundle": material,
                    },
                })
                rolled = await journal.snapshot("s")
                rolled_state = StateProjector().project_visible("s", rolled)
                self.assertEqual(
                    maker._compact.visible(rolled, rolled_state).visible_event_ids, ()
                )
                self.assertIsNone(LoopGuard().inspect(rolled, rolled[-1].sequence))
            finally:
                await assembly.scheduler.close()
