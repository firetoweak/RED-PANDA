from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import unittest

import pytest
from pydantic import ValidationError

from redpanda.assistant.context.projection import ModelContextProjector
from redpanda.assistant.compact.core import MODEL_USAGE
from redpanda.assistant.conversations import project_conversation
from redpanda.assistant.sessions import SessionView
from redpanda.assistant.work_plan import (
    UPDATE_PLAN, WORK_PLAN_CONTEXT, project_work_plan, update_plan_binding,
)
from redpanda.runtime import (
    Command, CommandOutcome, CommandOutcomeReceived, DispatchAttemptStarted,
    Event, InvokeTool, ModelDecision, OutcomeStatus, StateProjector, Step,
    StepCommitted, UserMessageReceived,
)
from redpanda.runtime.dispatcher import AttemptContext


PLAN = {
    "objective": "修复附件上传",
    "steps": [
        {"text": "定位原因", "status": "completed"},
        {"text": "修改与验证", "status": "in_progress"},
    ],
    "note": None,
}


def event(sequence, payload, *, causation_id=None):
    return Event(
        event_id=f"e{sequence}", session_id="s", sequence=sequence,
        payload=payload, occurred_at=datetime.now(timezone.utc),
        causation_id=causation_id, correlation_id=None, schema_version=5, artifact_refs=(),
    )


def update(sequence, plan, *, snapshot=None, ok=True):
    effect = InvokeTool(UPDATE_PLAN, (("plan", plan),))
    command = Command(f"c{sequence}", effect)
    return (
        event(sequence, StepCommitted(
            Step(
                f"step{sequence}", f"e{sequence - 1}", sequence - 1, "basis", sequence - 1,
                ModelDecision("调整计划", (effect,)), (command,),
            ),
            {
                MODEL_USAGE: {"window": None, "input_tokens": 10, "output_tokens": 5, "cached_input_tokens": 0},
                **({WORK_PLAN_CONTEXT: snapshot} if snapshot is not None else {}),
            },
        )),
        event(sequence + 1, DispatchAttemptStarted(f"a{sequence}", command.command_id),
              causation_id=f"e{sequence}"),
        event(sequence + 2, CommandOutcomeReceived(
            command.command_id, f"a{sequence}", CommandOutcome(
                OutcomeStatus.SUCCEEDED,
                value={"ok": ok, "code": "PLAN_UPDATED" if ok else "VALIDATION_ERROR",
                       "data": {"plan": plan}, "error": None, "hint": None},
            ),
        ), causation_id=f"e{sequence + 1}"),
    )


class WorkPlanToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_tool_validates_complete_versions_and_explicit_clear(self):
        binding = update_plan_binding()
        context = AttemptContext("s", "c", "a", 1)
        assert not binding.requires_authorization
        for plan in (PLAN, None):
            result = await binding.handler(context, {"plan": plan})
            assert result["ok"] is True
            assert result["data"] == {"plan": plan}
        bad_status = deepcopy(PLAN)
        bad_status["steps"][0]["status"] = "blocked"
        missing_note = {k: v for k, v in PLAN.items() if k != "note"}
        for arguments in ({}, {"plan": bad_status}, {"plan": missing_note},
                          {"plan": PLAN, "extra": True}, {"plan": {**PLAN, "steps": []}}):
            result = await binding.handler(context, arguments)
            assert result["ok"] is False
            assert result["code"] == "VALIDATION_ERROR"


def test_only_successful_committed_outcomes_change_current_plan():
    established = update(2, PLAN)
    assert project_work_plan(established[:2]) is None
    assert project_work_plan(established) == PLAN
    rejected = update(5, None, ok=False)
    assert project_work_plan(established + rejected) == PLAN
    cleared = established + rejected + update(8, None)
    assert project_work_plan(cleared) is None
    assert project_work_plan(cleared[:3]) == PLAN


def test_corrupt_persisted_plan_is_not_treated_as_an_empty_plan():
    with pytest.raises(ValidationError):
        project_work_plan(update(2, {"objective": "broken"}))


def test_request_prefix_and_historical_plan_text_survive_replace_and_clear():
    projector = ModelContextProjector()
    events = (event(1, UserMessageReceived("修复附件上传")), *update(2, PLAN))

    def prepare(history, *, visible=None, prefix=None):
        state = StateProjector().project_visible("s", history, visible)
        return projector.prepare(history, state, "s", "sys", prefix=prefix)

    first = prepare(events)
    assert first.messages[-1]["content"] == first.work_plan_context
    assert first.messages[0] == {"role": "system", "content": "sys"}
    changed = {**PLAN, "note": "新增边界验证"}
    events += update(5, changed, snapshot=first.work_plan_context)
    second = prepare(events)
    assert second.messages[:len(first.messages)] == first.messages
    assert second.work_plan_context != first.work_plan_context
    events += update(8, None, snapshot=second.work_plan_context)
    third = prepare(events)
    assert "当前计划已清除" in third.work_plan_context
    assert third.messages[-1]["content"] == third.work_plan_context
    assert third.messages[:len(second.messages)] == second.messages
    assert any(m["content"] == first.work_plan_context for m in third.messages)
    assert len(third.messages) == len(third.source_sequences)
    view = project_conversation(
        "s", events, StateProjector().project_visible("s", events).steps,
        session=SessionView("waiting", ("external_fact",), (), False),
    )
    assert view.work_plan is None
    assert len([tool for item in view.items if item.kind == "step" for tool in item.tools]) == 3


def test_compact_reading_window_keeps_plan_from_full_journal_not_summary():
    events = (event(1, UserMessageReceived("修复附件上传")), *update(2, PLAN))
    state = StateProjector().project_visible("s", events, ())
    prepared = ModelContextProjector().prepare(
        events, state, "s", "sys", prefix=[{"role": "user", "content": "摘要未提及计划"}],
    )
    assert len(prepared.messages) == 3
    assert "修复附件上传" in prepared.messages[-1]["content"]
    view = project_conversation(
        "s", events, StateProjector().project_visible("s", events).steps,
        session=SessionView("waiting", ("external_fact",), (), False),
    )
    assert view.work_plan == PLAN


def test_cleared_plan_overrides_old_plan_in_compact_handoff():
    projector = ModelContextProjector()
    events = (event(1, UserMessageReceived("修复附件上传")), *update(2, PLAN))
    previous = projector.prepare(
        events, StateProjector().project_visible("s", events), "s", "sys",
    )
    events += update(5, None, snapshot=previous.work_plan_context)
    handoff = [{"role": "user", "content": previous.work_plan_context}]
    prepared = projector.prepare(
        events, StateProjector().project_visible("s", events, ()),
        "s", "sys", prefix=handoff,
    )
    assert prepared.messages[1:2] == handoff
    assert "当前计划已清除" in prepared.work_plan_context
    assert prepared.messages[-1]["content"] == prepared.work_plan_context
    assert project_work_plan(events) is None


def test_no_plan_does_not_add_a_context_placeholder():
    events = (event(1, UserMessageReceived("你好")),)
    state = StateProjector().project_visible("s", events)
    prepared = ModelContextProjector().prepare(events, state, "s", "sys")
    assert prepared.work_plan_context is None
    assert not any(str(m["content"]).startswith("<work_plan>") for m in prepared.messages)
