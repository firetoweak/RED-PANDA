from __future__ import annotations

import unittest
from datetime import datetime, timezone

from konden.assistant.conversations import (
    UNKNOWN_TOOL_ERROR,
    project_conversation,
)
from konden.assistant.work_plan import UPDATE_PLAN, project_work_plan_updates
from konden.assistant.compact.core import MODEL_USAGE, WINDOW
from konden.assistant.sessions import SessionView
from konden.runtime import (
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
        schema_version=6,
        artifact_refs=artifact_refs,
    )


def timeline(events):
    """时间线消费的是归约后的回合，不是裸事件流。"""

    return StateProjector().project_visible("session-1", events).steps


def committed_step(event_id, trigger, content, commands):
    return StepCommitted(
        Step(
            step_id=event_id,
            trigger_event_ids=(trigger,),
            decision_cursor=1,
            basis_state_version="basis",
            observed_journal_position=1,
            decision=ModelDecision(content, tuple(command.effect for command in commands)),
            commands=commands,
        )
    )


def canonical_step_event(events, event_id, content, commands, metadata=None):
    frame = StateProjector().project("session-1", events).next_decision
    step = Step(
        step_id=f"decision-{event_id}",
        trigger_event_ids=frame.trigger_event_ids,
        decision_cursor=frame.decision_cursor,
        basis_state_version=frame.basis_state_version,
        observed_journal_position=frame.observed_journal_position,
        decision=ModelDecision(content, tuple(command.effect for command in commands)),
        commands=commands,
    )
    return event(events[-1].sequence + 1, event_id, StepCommitted(step, metadata),
                 causation_id=frame.trigger_event.event_id)


class ConversationProjectionTest(unittest.TestCase):
    def test_private_message_notices_follow_received_facts_and_delivered_outcomes(self):
        from konden.assistant.agents import PRIVATE_MESSAGE

        send = Command("send-1", InvokeTool("send_message", (
            ("recipient", "Ada"), ("text", "请检查代码"), ("reply_to", None),
        )))
        events = (
            event(1, "user-1", UserMessageReceived("问问 Ada")),
            event(2, "step-1", committed_step("decision-1", "user-1", "", (send,))),
            event(3, "incoming-1", DomainFactCommitted(PRIVATE_MESSAGE, {
                "message_id": "private-in", "sender": {"kind": "agent", "name": "Lin"},
                "recipient": "Atlas", "text": "我完成了检查", "reply_to": None,
                "thread_id": "private-in", "hop": 0, "origin": "origin-1",
            }, requests_decision=True)),
            event(4, "attempt-1", DispatchAttemptStarted("att-1", "send-1"), causation_id="step-1"),
            event(5, "outcome-1", CommandOutcomeReceived("send-1", "att-1", CommandOutcome(
                OutcomeStatus.SUCCEEDED, value={"ok": True, "code": "DELIVERED", "data": {
                    "message_id": "private-out", "recipient": "Ada",
                }},
            )), causation_id="attempt-1"),
            event(6, "step-2", committed_step("decision-2", "outcome-1", "已请 Ada 检查。", ())),
        )
        view = SessionView("waiting", ("external_fact",), (), False)
        pending = project_conversation("session-1", events[:4], timeline(events[:4]), session=view)
        self.assertEqual([item.direction for item in pending.items if item.kind == "private_message"],
                         ["received"])
        conversation = project_conversation("session-1", events, timeline(events), session=view)
        self.assertEqual([item.kind for item in conversation.items],
                         ["user", "step", "private_message", "private_message", "step"])
        self.assertEqual([(item.message_id, item.direction, item.peer, item.occurred_at)
                          for item in conversation.items if item.kind == "private_message"], [
            ("private-in", "received", "Lin", events[2].occurred_at),
            ("private-out", "sent", "Ada", events[4].occurred_at),
        ])

    def test_refused_private_message_is_only_a_tool_record(self):
        send = Command("send-1", InvokeTool("send_message", (("recipient", "Missing"),)))
        for code in ("RECIPIENT_NOT_FOUND", "SELF_MESSAGE", "INVALID_REPLY", "ORIGIN_LIMIT_REACHED"):
            with self.subTest(code=code):
                events = (
                    event(1, "step-1", committed_step("decision-1", "user-1", "", (send,))),
                    event(2, "attempt-1", DispatchAttemptStarted("att-1", "send-1"), causation_id="step-1"),
                    event(3, "outcome-1", CommandOutcomeReceived("send-1", "att-1", CommandOutcome(
                        OutcomeStatus.SUCCEEDED, value={"ok": False, "code": code, "error": "未送达"},
                    )), causation_id="attempt-1"),
                )
                conversation = project_conversation("session-1", events, timeline(events),
                    session=SessionView("waiting", ("external_fact",), (), False))
                self.assertEqual([item.kind for item in conversation.items], ["step"])
                self.assertEqual(conversation.items[0].tools[0].name, "send_message")

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


class _Workspaces:
    """查询只问 registry 一件事：这个工作区还在不在。"""

    def __init__(self, *workspace_ids: str) -> None:
        self._workspace_ids = set(workspace_ids)

    def contains(self, workspace_id: str) -> bool:
        return workspace_id in self._workspace_ids


class Idle:
    workspaces = _Workspaces("workspace-1")
    auto_authorize_enabled = True

    def activity(self, session_id):
        return "idle"

    def auto_authorize(self):
        return self.auto_authorize_enabled

    def current_failure(self, session_id):
        return None

    def conversation_status(self, session_id):
        from konden.assistant.compact.store import ConversationStatus

        return ConversationStatus(session_id, session_id, 0, None)

    def next_scheduled_check(self, session_id):
        return None

    def absence_watches(self):
        return ()

    async def view(self, session_id):
        raise AssertionError("读会话不得唤醒 Worker")


class QueriesTest(unittest.IsolatedAsyncioTestCase):
    async def test_conversation_carries_host_session_flags(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from konden.assistant.conversations import AssistantQueries
        from konden.assistant.host.session_store import SessionStore
        from konden.runtime import SqliteJournal
        from konden.runtime.events import DeliveryIdentity, EventDraft

        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            queries = AssistantQueries(store, Idle())
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
            spoken = await queries.conversation("spoken")

        self.assertTrue(spoken.session.auto_authorize)

    async def test_inbox_lists_only_agent_pending_items(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from konden.assistant.agents import AGENT_CREATED
        from konden.assistant.compact.core import MODEL_USAGE
        from konden.assistant.control import (
            CONTROL_PROPOSED, CONTROL_REQUEST_METADATA, CONTROL_SOURCE,
        )
        from konden.assistant.conversations import AssistantQueries
        from konden.assistant.host.session_store import SessionStore
        from konden.runtime import (
            Command, DomainFactCommitted, InvokeTool, ModelDecision, SqliteJournal,
            StateProjector, Step, StepClaimRequest, StepCommitted,
        )
        from konden.runtime.events import DeliveryIdentity, EventDraft

        async def wake(store, session_id: str, payload: DomainFactCommitted, source: str) -> None:
            await SqliteJournal(store.require(session_id)).accept_delivery(
                EventDraft(
                    event_id=f"wake-{session_id}",
                    session_id=session_id,
                    payload=payload,
                    occurred_at=datetime(2026, 9, 16, tzinfo=timezone.utc),
                    delivery=DeliveryIdentity(source, f"wake-{session_id}"),
                )
            )

        async def commit(store, session_id: str, commands=(), metadata=None) -> None:
            journal = SqliteJournal(store.require(session_id))
            frame = StateProjector().project(
                session_id, await journal.snapshot(session_id),
            ).next_decision
            lease = await journal.acquire_step(
                StepClaimRequest(
                    session_id,
                    frame.trigger_event_ids,
                    frame.decision_cursor,
                    frame.basis_state_version,
                    frame.observed_journal_position,
                ),
                token=f"claim-{session_id}",
                owner_id="worker-1",
                lease_seconds=30,
            )
            await journal.commit_step(
                lease,
                EventDraft(
                    event_id=f"step-{session_id}",
                    session_id=session_id,
                    payload=StepCommitted(
                        Step(
                            f"decision-{session_id}",
                            frame.trigger_event_ids,
                            frame.decision_cursor,
                            frame.basis_state_version,
                            frame.observed_journal_position,
                            ModelDecision("等待决定", tuple(command.effect for command in commands)),
                            commands,
                        ),
                        metadata or {
                            MODEL_USAGE: {
                                "window": None, "input_tokens": 1,
                                "output_tokens": 1, "cached_input_tokens": 0,
                            },
                        },
                    ),
                    occurred_at=datetime(2026, 9, 16, tzinfo=timezone.utc),
                    causation_id=frame.trigger_event.event_id,
                ),
            )

        async def propose(store, session_id: str, request_id: str) -> None:
            await SqliteJournal(store.require(session_id)).accept_delivery(
                EventDraft(
                    event_id=f"proposed-{request_id}",
                    session_id=session_id,
                    payload=DomainFactCommitted(
                        CONTROL_PROPOSED,
                        {
                            "request_id": request_id,
                            "action": "workspace.remove",
                            "payload": {"workspace_id": "workspace-1"},
                            "summary": "删除工作区",
                            "risk": "high",
                        },
                    ),
                    occurred_at=datetime(2026, 9, 16, tzinfo=timezone.utc),
                    delivery=DeliveryIdentity(CONTROL_SOURCE, f"{request_id}:proposed"),
                )
            )

        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            sessions = Idle()
            queries = AssistantQueries(store, sessions)
            for session_id in ("quiet", "named", "old"):
                await store.create(session_id, directory=Path(directory))
            await wake(
                store, "quiet",
                DomainFactCommitted(
                    AGENT_CREATED,
                    {
                        "name": "安静熊猫",
                        "directory": str(Path(directory).resolve()),
                    },
                    requests_decision=True,
                ),
                "coding_bot",
            )
            await commit(store, "quiet", commands=(
                Command("cmd-quiet", InvokeTool("apply_patch", (("path", "note.txt"),)), requires_authorization=True),
            ))
            await wake(
                store, "named",
                DomainFactCommitted(
                    AGENT_CREATED,
                    {
                        "name": "验证熊猫",
                        "directory": str(Path(directory).resolve()),
                    },
                    requests_decision=True,
                ),
                "coding_bot",
            )
            await commit(store, "named", metadata={
                CONTROL_REQUEST_METADATA: {
                    "request_id": "req-named",
                    "tool": "propose_workspace_remove",
                    "action": "workspace.remove",
                    "arguments": {"workspace_id": "workspace-1"},
                },
                MODEL_USAGE: {
                    "window": None, "input_tokens": 1,
                    "output_tokens": 1, "cached_input_tokens": 0,
                },
            })
            await propose(store, "named", "req-named")
            await wake(store, "old", DomainFactCommitted("test.wake", {}, requests_decision=True), "test")
            await commit(store, "old", metadata={
                CONTROL_REQUEST_METADATA: {
                    "request_id": "req-old",
                    "tool": "propose_workspace_remove",
                    "action": "workspace.remove",
                    "arguments": {"workspace_id": "workspace-1"},
                },
                MODEL_USAGE: {
                    "window": None, "input_tokens": 1,
                    "output_tokens": 1, "cached_input_tokens": 0,
                },
            })
            await propose(store, "old", "req-old")
            sessions.auto_authorize_enabled = False
            inbox = await queries.list_inbox()

            # Read the exact interval between issuing an ask command and recording
            # its automatic authorization; no Worker is running to close it.
            journal = SqliteJournal(store.require("quiet"))
            before = await journal.snapshot("quiet")
            for enabled in (True, False):
                sessions.auto_authorize_enabled = enabled
                conversation = await queries.conversation("quiet")
                self.assertEqual(conversation.session.pending_authorization_ids,
                                 () if enabled else ("cmd-quiet",))
                self.assertEqual(len(conversation.session.pending_authorization_commands),
                                 0 if enabled else 1)
                self.assertEqual(conversation.items[0].tools[0].status,
                                 "queued" if enabled else "awaiting_authorization")
                self.assertEqual((await queries.agent_entry("quiet")).status,
                                 "idle" if enabled else "authorization")
                current_inbox = await queries.list_inbox()
                self.assertEqual([item.kind for item in current_inbox],
                                 ["control"] if enabled else ["authorization", "control"])
                self.assertEqual(await journal.snapshot("quiet"), before)

        self.assertEqual(
            [(item.kind, item.label, item.session_id) for item in inbox],
            [("authorization", "安静熊猫", "quiet"), ("control", "验证熊猫", "named")],
        )
        self.assertEqual(inbox[0].name, "apply_patch")
        self.assertEqual(inbox[0].arguments, {"path": "note.txt"})
        self.assertEqual(inbox[1].summary, "删除工作区")
        self.assertEqual(inbox[1].risk, "high")

    async def test_inbox_reports_limits_but_pending_messages_do_not_require_human_continuation(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from konden.assistant.agent_messages import LIMIT_NOTICE
        from konden.assistant.agents import AGENT_CREATED, CommunicationLimitReached, fact_arguments
        from konden.assistant.conversations import AssistantQueries
        from konden.assistant.host.session_store import SessionStore
        from konden.runtime import DomainFactCommitted, SqliteJournal, UserMessageReceived
        from konden.runtime.events import DeliveryIdentity, EventDraft

        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            queries = AssistantQueries(store, Idle())
            for session_id, name in (("limited", "上限熊猫"), ("pending", "待处理熊猫")):
                await store.create(session_id, directory=Path(directory))
                await SqliteJournal(store.require(session_id)).accept_delivery(EventDraft(
                    event_id=f"created-{session_id}", session_id=session_id,
                    payload=DomainFactCommitted(
                        AGENT_CREATED, {"name": name, "directory": str(Path(directory).resolve())},
                        requests_decision=True),
                    occurred_at=datetime(2026, 10, 19, tzinfo=timezone.utc),
                    delivery=DeliveryIdentity("coding_bot", f"created-{session_id}"),
                ))
            journal = SqliteJournal(store.require("limited"))
            await journal.accept_delivery(EventDraft(
                event_id="limit-1", session_id="limited",
                payload=DomainFactCommitted(
                    **{key: fact_arguments(
                        CommunicationLimitReached(recipient="Lin", text=LIMIT_NOTICE), delivery_id="limit-1",
                    )[key] for key in ("fact_type", "data", "requests_decision")}),
                occurred_at=datetime(2026, 10, 19, 1, tzinfo=timezone.utc),
                delivery=DeliveryIdentity("coding_bot", "limit-1"),
            ))
            await SqliteJournal(store.require("pending")).accept_delivery(EventDraft(
                event_id="user-pending", session_id="pending",
                payload=UserMessageReceived("还有事"),
                occurred_at=datetime(2026, 10, 19, 1, tzinfo=timezone.utc),
                delivery=DeliveryIdentity("web", "user-pending"),
            ))
            inbox = await queries.list_inbox()

        kinds = [(item.kind, item.label) for item in inbox]
        self.assertEqual(kinds, [("limit", "上限熊猫")])
        self.assertEqual(inbox[0].recipient, "Lin")

    async def test_conversation_projects_control_approval_from_journal(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from konden.assistant.control import (
            CONTROL_PROPOSED,
            CONTROL_REQUEST_METADATA,
            CONTROL_SOURCE,
        )
        from konden.assistant.conversations import AssistantQueries
        from konden.assistant.host.session_store import SessionStore
        from konden.runtime import DomainFactCommitted, SqliteJournal, StepClaimRequest
        from konden.runtime.events import DeliveryIdentity, EventDraft

        class Host:
            def activity(self, session_id):
                return "idle"

            def auto_authorize(self):
                return None

            def current_failure(self, session_id):
                return None

            def conversation_status(self, session_id):
                from konden.assistant.compact.store import ConversationStatus

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
                    frame.trigger_event_ids,
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
                            frame.trigger_event_ids,
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
