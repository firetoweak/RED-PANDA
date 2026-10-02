from __future__ import annotations

import unittest
from datetime import datetime, timezone

from helperme.assistant.conversations import (
    UNKNOWN_TOOL_ERROR,
    project_conversation,
    project_session_summary,
)
from helperme.assistant.workspaces import workspace_binding
from helperme.assistant.work_plan import UPDATE_PLAN, project_work_plan_updates
from helperme.assistant.compact.core import MODEL_USAGE, WINDOW
from helperme.assistant.sessions import SessionView
from helperme.assistant.subagent.subagent import DELEGATE, REPORT_FACT, child_session_id
from helperme.runtime import (
    Command,
    CommandOutcome,
    CommandOutcomeReceived,
    CommandRejected,
    DispatchAttemptStarted,
    DomainFactCommitted,
    Event,
    InvokeTool,
    ModelDecision,
    OutcomeStatus,
    StateProjector,
    Step,
    StepCommitted,
    UserMessageReceived,
)


def event(sequence, event_id, payload, causation_id=None, artifact_refs=()):
    return Event(
        event_id=event_id,
        session_id="session-1",
        sequence=sequence,
        payload=payload,
        occurred_at=datetime(2026, 9, 15, sequence, tzinfo=timezone.utc),
        causation_id=causation_id,
        correlation_id=None,
        schema_version=5,
        artifact_refs=artifact_refs,
    )


def timeline(events):
    """时间线消费的是归约后的回合，不是裸事件流。"""

    return StateProjector().project_visible("session-1", events).steps


def committed_step(event_id, trigger, content, commands):
    return StepCommitted(
        Step(
            step_id=event_id,
            trigger_event_id=trigger,
            decision_cursor=1,
            basis_state_version="basis",
            observed_journal_position=1,
            decision=ModelDecision(content, tuple(command.effect for command in commands)),
            commands=commands,
        )
    )


class ConversationProjectionTest(unittest.TestCase):
    def test_plan_history_keeps_step_identity_after_clear_and_replacement(self):
        completed = {"objective": "第一项任务", "steps": [{"text": "验证", "status": "completed"}], "note": None}
        active = {"objective": "第二项任务", "steps": [{"text": "调查", "status": "in_progress"}], "note": None}
        events = [event(1, "user-1", UserMessageReceived("第一项任务"))]
        for index, plan in enumerate((completed, None, active)):
            sequence = 2 + index * 3
            command = Command(f"plan-{index}", InvokeTool(UPDATE_PLAN, (("plan", plan),)))
            step = committed_step(f"decision-{index}", "user-1", "记录计划", (command,))
            events.extend((
                event(sequence, f"step-{index}", StepCommitted(step.step, {
                    MODEL_USAGE: {"window": None, "input_tokens": 10, "output_tokens": 5, "cached_input_tokens": 0},
                })),
                event(sequence + 1, f"dispatch-{index}", DispatchAttemptStarted(f"attempt-{index}", command.command_id),
                      causation_id=f"step-{index}"),
                event(sequence + 2, f"outcome-{index}", CommandOutcomeReceived(
                    command.command_id, f"attempt-{index}", CommandOutcome(OutcomeStatus.SUCCEEDED, value={
                        "ok": True, "code": "PLAN_UPDATED", "data": {"plan": plan},
                    }),
                ), causation_id=f"dispatch-{index}"),
            ))
        conversation = project_conversation(
            "session-1", tuple(events), timeline(tuple(events)),
            session=SessionView("waiting", ("external_fact",), (), False),
        )
        self.assertEqual(conversation.work_plan, active)
        self.assertEqual(
            [(update.step_id, update.plan) for update in conversation.work_plan_updates],
            [("decision-0", completed), ("decision-1", None), ("decision-2", active)],
        )
        # 未提交结果的请求不能进入历史计划投影。
        self.assertEqual(project_work_plan_updates(tuple(events[:3])), ())

    def test_projects_committed_input_usage_including_cached_tokens(self):
        step = committed_step("decision-1", "user-1", "回复", ())
        events = (
            event(1, "user-1", UserMessageReceived("你好")),
            event(2, "step-1", StepCommitted(step.step, {
                MODEL_USAGE: {"window": None, "input_tokens": 1500,
                              "output_tokens": 20, "cached_input_tokens": 1200},
            })),
        )
        conversation = project_conversation(
            "session-1", events, timeline(events),
            session=SessionView("waiting", ("external_fact",), (), False),
        )
        self.assertEqual(conversation.context_input_tokens, 1500)

    def test_window_rollover_clears_usage_until_new_window_has_a_committed_call(self):
        step = committed_step("decision-1", "user-1", "回复", ())
        events = (
            event(1, "user-1", UserMessageReceived("你好")),
            event(2, "step-1", StepCommitted(step.step, {
                MODEL_USAGE: {"window": None, "input_tokens": 1500,
                              "output_tokens": 20, "cached_input_tokens": 1200},
            })),
            event(3, "window-1", DomainFactCommitted(WINDOW, {"id": "w1", "parent": None})),
        )
        view = SessionView("waiting", ("external_fact",), (), False)
        before_call = project_conversation("session-1", events, timeline(events), session=view)
        self.assertIsNone(before_call.context_input_tokens)
        new_step = committed_step("decision-2", "user-2", "新的回复", ())
        events += (
            event(4, "user-2", UserMessageReceived("继续")),
            event(5, "step-2", StepCommitted(new_step.step, {
                MODEL_USAGE: {"window": "w1", "input_tokens": 500,
                              "output_tokens": 10, "cached_input_tokens": 0},
            })),
        )
        after_call = project_conversation("session-1", events, timeline(events), session=view)
        self.assertEqual(after_call.context_input_tokens, 500)

    def test_conversation_without_model_calls_has_unknown_usage(self):
        conversation = project_conversation(
            "session-1", (), (), session=SessionView("waiting", ("external_fact",), (), False),
        )
        self.assertIsNone(conversation.context_input_tokens)

    def test_projects_each_decision_as_one_step_with_its_tools(self):
        read = Command("cmd-read", InvokeTool("read_file"))
        deliver = Command("cmd-deliver", InvokeTool("deliver"))
        events = (
            event(1, "user-1", UserMessageReceived("你好")),
            event(
                2,
                "step-1",
                committed_step(
                    "decision-1",
                    "user-1",
                    "  世界  ",
                    (read, deliver),
                ),
            ),
            event(
                3,
                "attempt-1",
                DispatchAttemptStarted("att-1", "cmd-read"),
                causation_id="step-1",
            ),
            event(
                4,
                "out-1",
                CommandOutcomeReceived(
                    "cmd-read",
                    "att-1",
                    CommandOutcome(OutcomeStatus.SUCCEEDED, value="ok"),
                ),
                causation_id="attempt-1",
            ),
        )
        view = SessionView("waiting", ("external_fact",), (), False)

        conversation = project_conversation(
            "session-1", events, timeline(events), session=view
        )

        self.assertIsNone(conversation.workspace_id)
        self.assertEqual(conversation.revision, 4)
        self.assertEqual(conversation.items[0].kind, "user")
        step = conversation.items[1]
        self.assertEqual(step.kind, "step")
        self.assertEqual(step.step_id, "decision-1")
        self.assertEqual(step.output_id, "user-1")
        self.assertEqual(step.text, "世界")
        self.assertEqual(len(step.tools), 1)
        self.assertEqual(step.tools[0].command_id, "cmd-read")
        self.assertEqual(step.tools[0].name, "read_file")
        self.assertEqual(step.tools[0].status, "succeeded")
        self.assertIsNone(step.thinking)

    def test_projects_the_bound_workspace(self):
        events = (
            event(1, "ws-1", workspace_binding("workspace-demo")),
            event(2, "user-1", UserMessageReceived("你好")),
        )
        conversation = project_conversation(
            "session-1",
            events,
            timeline(events),
            session=SessionView("waiting", ("external_fact",), (), False),
        )
        self.assertEqual(conversation.workspace_id, "workspace-demo")

    def test_projects_reasoning_content_as_step_thinking(self):
        events = (
            event(1, "user-1", UserMessageReceived("你好")),
            event(
                2,
                "step-1",
                StepCommitted(
                    Step(
                        step_id="decision-1",
                        trigger_event_id="user-1",
                        decision_cursor=1,
                        basis_state_version="basis",
                        observed_journal_position=1,
                        decision=ModelDecision("世界"),
                        commands=(),
                    ),
                    {
                        "message_extensions": {
                            "reasoning_content": "  先确认目标  ",
                        },
                        MODEL_USAGE: {"window": None, "input_tokens": 1200,
                                      "output_tokens": 10, "cached_input_tokens": 1000},
                    },
                ),
            ),
        )
        conversation = project_conversation(
            "session-1",
            events,
            timeline(events),
            session=SessionView("waiting", ("external_fact",), (), False),
        )
        self.assertEqual(conversation.items[1].thinking, "先确认目标")

    def test_tool_without_attempt_is_queued_and_failed_outcome_keeps_error(self):
        search = Command("cmd-search", InvokeTool("web_search"))
        events = (
            event(
                1,
                "step-1",
                committed_step("decision-1", "user-1", "", (search,)),
            ),
        )
        view = SessionView("waiting", ("external_fact",), (), False)

        queued = project_conversation(
            "session-1", events, timeline(events), session=view
        )
        self.assertEqual(queued.items[0].kind, "step")
        self.assertEqual(queued.items[0].tools[0].status, "queued")
        self.assertIsNone(queued.items[0].tools[0].error)

        ran = events + (
            event(
                2,
                "attempt-1",
                DispatchAttemptStarted("att-1", "cmd-search"),
                causation_id="step-1",
            ),
            event(
                3,
                "out-1",
                CommandOutcomeReceived(
                    "cmd-search",
                    "att-1",
                    CommandOutcome(OutcomeStatus.FAILED, error_message="boom"),
                ),
                causation_id="attempt-1",
            ),
        )
        failed = project_conversation(
            "session-1", ran, timeline(ran), session=view
        )
        self.assertEqual(failed.items[0].tools[0].status, "failed")
        self.assertEqual(failed.items[0].tools[0].error, "boom")

    def test_unknown_attempt_never_uses_session_activity_as_terminal_evidence(self):
        search = Command("cmd-search", InvokeTool("web_search"))
        events = (
            event(
                1,
                "step-1",
                committed_step("decision-1", "user-1", "", (search,)),
            ),
            event(
                2,
                "attempt-1",
                DispatchAttemptStarted("att-1", "cmd-search"),
                causation_id="step-1",
            ),
        )
        view = SessionView("waiting", ("command:cmd-search",), (), False)

        conversation = project_conversation(
            "session-1", events, timeline(events), session=view
        )
        self.assertEqual(conversation.items[0].tools[0].status, "unknown")
        self.assertEqual(
            conversation.items[0].tools[0].error,
            UNKNOWN_TOOL_ERROR,
        )

    def test_interrupted_command_is_unknown_with_its_own_error(self):
        search = Command("cmd-search", InvokeTool("execute_command"))
        events = (
            event(
                1,
                "step-1",
                committed_step("decision-1", "user-1", "", (search,)),
            ),
            event(
                2,
                "attempt-1",
                DispatchAttemptStarted("att-1", "cmd-search"),
                causation_id="step-1",
            ),
            event(
                3,
                "out-1",
                CommandOutcomeReceived(
                    "cmd-search",
                    "att-1",
                    CommandOutcome(
                        OutcomeStatus.SUCCEEDED,
                        value={
                            "ok": None,
                            "code": "COMMAND_INTERRUPTED",
                            "data": None,
                            "error": "命令已被打断，执行结果未知。",
                            "hint": "证据",
                        },
                    ),
                ),
                causation_id="attempt-1",
            ),
        )
        conversation = project_conversation(
            "session-1",
            events,
            timeline(events),
            session=SessionView("waiting", ("external_fact",), (), False),
        )
        self.assertEqual(conversation.items[0].tools[0].status, "unknown")
        self.assertEqual(
            conversation.items[0].tools[0].error,
            "命令已被打断，执行结果未知。",
        )

    def test_summary_uses_first_user_message_and_last_user_message_time(self):
        events = (
            event(1, "user-1", UserMessageReceived("第一行\n第二行")),
            event(2, "user-2", UserMessageReceived("之后")),
            event(
                3, "step-2",
                committed_step("decision-2", "user-2", "回复", ()),
            ),
        )

        summary = project_session_summary(
            "session-1",
            events,
            workspace_id="workspace-1",
            activity="running",
        )

        self.assertEqual(summary.title, "第一行")
        self.assertEqual(summary.updated_at, events[1].occurred_at)
        self.assertEqual(summary.activity, "running")
        self.assertFalse(summary.has_active_subagents)
        self.assertEqual(
            project_session_summary(
                "session-1",
                events,
                workspace_id="workspace-1",
                activity="running",
                title="我起的名",
            ).title,
            "我起的名",
        )

    def test_idle_summary_keeps_subagent_active_until_report_is_received(self):
        child_id = child_session_id("session-1", "delegate-1")
        command = Command("delegate-1", InvokeTool(DELEGATE, (("task", "调查问题"),)))
        events = (
            event(1, "user-1", UserMessageReceived("调查问题")),
            event(2, "step-1", committed_step("decision-1", "user-1", "", (command,))),
            event(3, "dispatch-1", DispatchAttemptStarted("attempt-1", command.command_id)),
            event(4, "outcome-1", CommandOutcomeReceived(
                command.command_id, "attempt-1", CommandOutcome(OutcomeStatus.SUCCEEDED, value={
                    "ok": True, "code": "DELEGATED", "data": {"child_session_id": child_id},
                }),
            )),
            event(5, "report-1", DomainFactCommitted(REPORT_FACT, {"child_session_id": child_id})),
        )
        for snapshot, active in ((events[:4], True), (events, False)):
            with self.subTest(active=active):
                summary = project_session_summary(
                    "session-1", snapshot, workspace_id="workspace-1", activity="idle",
                )
                self.assertEqual(summary.activity, "idle")
                self.assertEqual(summary.has_active_subagents, active)

    def test_user_message_carries_image_refs(self):
        attachment_id = "sha256:" + "a" * 64
        events = (
            event(
                1,
                "user-1",
                UserMessageReceived("[Image #1]"),
                artifact_refs=(attachment_id,),
            ),
        )
        conversation = project_conversation(
            "session-1",
            events,
            timeline(events),
            session=SessionView("waiting", ("external_fact",), (), False),
        )

        self.assertEqual(conversation.items[0].kind, "user")
        self.assertEqual(conversation.items[0].text, "[Image #1]")
        self.assertEqual(conversation.items[0].images, (attachment_id,))

    def test_pending_authorization_and_rejection_are_distinct_statuses(self):
        write = Command(
            "cmd-write",
            InvokeTool("write_file", (("path", "a.md"), ("content", "x"))),
            requires_authorization=True,
        )
        events = (
            event(
                1,
                "step-1",
                committed_step("decision-1", "user-1", "", (write,)),
            ),
        )
        waiting = project_conversation(
            "session-1",
            events,
            timeline(events),
            session=SessionView(
                "waiting",
                ("authorization:cmd-write",),
                ("cmd-write",),
                False,
            ),
        )
        self.assertEqual(waiting.items[0].tools[0].status, "awaiting_authorization")
        self.assertEqual(waiting.items[0].tools[0].arguments, {"path": "a.md", "content": "x"})

        turned_down = events + (
            event(2, "reject-1", CommandRejected("cmd-write"), causation_id="step-1"),
        )
        rejected = project_conversation(
            "session-1",
            turned_down,
            timeline(turned_down),
            session=SessionView("waiting", ("external_fact",), (), False),
        )
        self.assertEqual(rejected.items[0].tools[0].status, "rejected")


class Idle:
    superseded: frozenset[str] = frozenset()

    def activity(self, session_id):
        return "idle"

    def auto_authorize(self, session_id):
        return session_id == "spoken"

    def is_paused(self, session_id):
        return session_id == "spoken"

    def is_superseded(self, session_id):
        return session_id in self.superseded

    def is_archived(self, session_id):
        return False

    def session_title(self, session_id):
        return None

    def conversation_status(self, session_id):
        from helperme.assistant.compact.store import ConversationStatus

        return ConversationStatus(session_id, session_id, 0, None)

    def next_scheduled_check(self, session_id):
        return None

    async def view(self, session_id):
        raise AssertionError("读会话不得唤醒 Worker")


class ListSessionsTest(unittest.IsolatedAsyncioTestCase):
    async def test_omits_journals_without_user_messages(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from helperme.assistant.conversations import AssistantQueries
        from helperme.assistant.host.session_store import SessionStore
        from helperme.runtime import SqliteJournal
        from helperme.runtime.events import DeliveryIdentity, EventDraft

        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            queries = AssistantQueries(store, Idle())
            await store.create("empty", workspace_id="workspace-1")
            await store.create("spoken", workspace_id="workspace-1")
            await SqliteJournal(store.require("spoken")).accept_delivery(
                EventDraft(
                    event_id="user-1",
                    session_id="spoken",
                    payload=UserMessageReceived("你好"),
                    occurred_at=datetime(2026, 9, 16, tzinfo=timezone.utc),
                    delivery=DeliveryIdentity("web", "d1"),
                )
            )
            listed = await queries.list_sessions()
            spoken = await queries.conversation("spoken")

        self.assertEqual([item.session_id for item in listed], ["spoken"])
        self.assertEqual(listed[0].title, "你好")
        self.assertTrue(spoken.session.auto_authorize)
        self.assertTrue(spoken.session.paused)

    async def test_superseded_identities_stay_out_of_the_top_level_list(self):
        """被改写顶掉的身份照常可读，只是不再作为一条会话出现在列表里。"""
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from helperme.assistant.conversations import AssistantQueries
        from helperme.assistant.host.session_store import SessionStore
        from helperme.runtime import SqliteJournal
        from helperme.runtime.events import DeliveryIdentity, EventDraft

        class Rewritten(Idle):
            superseded = frozenset({"spoken"})

        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            queries = AssistantQueries(store, Rewritten())
            await store.create("spoken", workspace_id="workspace-1")
            await SqliteJournal(store.require("spoken")).accept_delivery(
                EventDraft(
                    event_id="user-1",
                    session_id="spoken",
                    payload=UserMessageReceived("你好"),
                    occurred_at=datetime(2026, 9, 16, tzinfo=timezone.utc),
                    delivery=DeliveryIdentity("web", "d1"),
                )
            )

            self.assertEqual(await queries.list_sessions(), ())
            self.assertEqual((await queries.conversation("spoken")).items[0].text, "你好")

    async def test_archived_identities_stay_out_of_the_top_level_list(self):
        """归档只是不再出现在列表里，会话仍可读。"""
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from helperme.assistant.conversations import AssistantQueries
        from helperme.assistant.host.session_store import SessionStore
        from helperme.runtime import SqliteJournal
        from helperme.runtime.events import DeliveryIdentity, EventDraft

        class Archived(Idle):
            def is_archived(self, session_id):
                return session_id == "spoken"

        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            queries = AssistantQueries(store, Archived())
            await store.create("spoken", workspace_id="workspace-1")
            await SqliteJournal(store.require("spoken")).accept_delivery(
                EventDraft(
                    event_id="user-1",
                    session_id="spoken",
                    payload=UserMessageReceived("你好"),
                    occurred_at=datetime(2026, 9, 16, tzinfo=timezone.utc),
                    delivery=DeliveryIdentity("web", "d1"),
                )
            )

            self.assertEqual(await queries.list_sessions(), ())
            self.assertEqual((await queries.conversation("spoken")).items[0].text, "你好")

    async def test_listed_title_uses_the_host_override(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from helperme.assistant.conversations import AssistantQueries
        from helperme.assistant.host.session_store import SessionStore
        from helperme.runtime import SqliteJournal
        from helperme.runtime.events import DeliveryIdentity, EventDraft

        class Named(Idle):
            def session_title(self, session_id):
                return "我起的名"

        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            queries = AssistantQueries(store, Named())
            await store.create("spoken", workspace_id="workspace-1")
            await SqliteJournal(store.require("spoken")).accept_delivery(
                EventDraft(
                    event_id="user-1",
                    session_id="spoken",
                    payload=UserMessageReceived("你好"),
                    occurred_at=datetime(2026, 9, 16, tzinfo=timezone.utc),
                    delivery=DeliveryIdentity("web", "d1"),
                )
            )
            listed = await queries.list_sessions()

        self.assertEqual(listed[0].title, "我起的名")

    async def test_conversation_projects_control_approval_from_journal(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from helperme.assistant.control import (
            CONTROL_PROPOSED,
            CONTROL_REQUEST_METADATA,
            CONTROL_SOURCE,
        )
        from helperme.assistant.conversations import AssistantQueries
        from helperme.assistant.host.session_store import SessionStore
        from helperme.runtime import DomainFactCommitted, SqliteJournal, StepClaimRequest
        from helperme.runtime.events import DeliveryIdentity, EventDraft

        class Host:
            def activity(self, session_id):
                return "idle"

            def auto_authorize(self, session_id):
                return None

            def is_paused(self, session_id):
                return None

            def conversation_status(self, session_id):
                from helperme.assistant.compact.store import ConversationStatus

                return ConversationStatus(session_id, session_id, 2, "failed")

            def next_scheduled_check(self, session_id):
                return None

            async def view(self, session_id):
                raise AssertionError("读会话不得唤醒 Worker")

        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            queries = AssistantQueries(store, Host())
            await store.create("spoken", workspace_id="workspace-1")
            journal = SqliteJournal(store.require("spoken"))
            await journal.accept_delivery(
                EventDraft(
                    event_id="user-1",
                    session_id="spoken",
                    payload=UserMessageReceived("删掉它"),
                    occurred_at=datetime(2026, 9, 16, tzinfo=timezone.utc),
                    delivery=DeliveryIdentity("web", "d1"),
                )
            )
            frame = StateProjector().project(
                "spoken", await journal.snapshot("spoken")
            ).next_decision
            lease = await journal.acquire_step(
                StepClaimRequest(
                    "spoken",
                    frame.trigger_event.event_id,
                    frame.decision_cursor,
                    frame.basis_state_version,
                    frame.observed_journal_position,
                ),
                token="claim-1",
                owner_id="worker-1",
                lease_seconds=30,
            )
            await journal.commit_step(
                lease,
                EventDraft(
                    event_id="step-event-1",
                    session_id="spoken",
                    payload=StepCommitted(
                        Step(
                            "step-1",
                            frame.trigger_event.event_id,
                            frame.decision_cursor,
                            frame.basis_state_version,
                            frame.observed_journal_position,
                            ModelDecision("准备控制提案"),
                            (),
                        ),
                        {
                            CONTROL_REQUEST_METADATA: {
                                "request_id": "req-1",
                                "tool": "propose_workspace_remove",
                                "action": "workspace.remove",
                                "arguments": {"workspace_id": "workspace-1"},
                            },
                            MODEL_USAGE: {"window": None, "input_tokens": 1500,
                                          "output_tokens": 10, "cached_input_tokens": 1000},
                        },
                    ),
                    occurred_at=datetime(2026, 9, 16, tzinfo=timezone.utc),
                    causation_id=frame.trigger_event.event_id,
                ),
            )
            await journal.accept_delivery(
                EventDraft(
                    event_id="proposed-1",
                    session_id="spoken",
                    payload=DomainFactCommitted(
                        CONTROL_PROPOSED,
                        {
                            "request_id": "req-1",
                            "action": "workspace.remove",
                            "payload": {"workspace_id": "workspace-1"},
                            "summary": "删除工作区",
                            "risk": "high",
                        },
                    ),
                    occurred_at=datetime(2026, 9, 16, tzinfo=timezone.utc),
                    delivery=DeliveryIdentity(CONTROL_SOURCE, "req-1:proposed"),
                )
            )
            conversation = await queries.conversation("spoken")

        self.assertEqual(conversation.session.control_approval.request_id, "req-1")
        self.assertEqual(conversation.session.control_approval.risk, "high")
        self.assertEqual(conversation.compact_count, 2)
        self.assertEqual(conversation.compact_phase, "failed")
        self.assertEqual(conversation.context_input_tokens, 1500)
