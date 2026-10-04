from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from hashlib import sha256

from redpanda.runtime.codec import EVENT_SCHEMA_VERSION
from redpanda.runtime.events import (
    CommandAuthorized,
    CommandRejected,
    CommandOutcomeReceived,
    DecisionCancelled,
    DomainFactCommitted,
    DispatchAttemptStarted,
    Event,
    StepContinuationCancelled,
    StepCommitted,
    UserMessageReceived,
)
from redpanda.runtime.model import (
    AttemptPhase,
    AttemptState,
    CanonicalState,
    CommandPhase,
    CommandState,
    DecisionState,
    RuntimeStatus,
    Step,
    StepState,
)


@dataclass(frozen=True, slots=True)
class DecisionFrame:
    trigger_event: Event
    state: DecisionState
    decision_cursor: int
    basis_state_version: str
    observed_journal_position: int


@dataclass(frozen=True, slots=True)
class RuntimeProjection:
    state: CanonicalState
    next_decision: DecisionFrame | None


class _StateBuilder:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.user_messages: list[str] = []
        self.commands: dict[str, CommandState] = {}
        self.step_events: list[Event] = []
        self.step_ids: set[str] = set()
        self.visible_event_ids: list[str] = []
        self.attempt_event_ids: dict[tuple[str, str], str] = {}
        self.dispatch_command_ids: dict[str, str] = {}
        self.canonical_outcome_event_ids: set[str] = set()

    def version(self) -> str:
        content = json.dumps(
            self.visible_event_ids,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(content).hexdigest()

    def step_states(
        self,
        commands: Mapping[str, CommandState],
    ) -> tuple[StepState, ...]:
        """按提交顺序把回合装配成聚合根。

        命令来源必须显式给出：冻结帧看到的命令可能停在触发点之前，而
        CanonicalState 报告的是最新终局。同一份状态里混用两者，同一个
        命令会带着两个 phase 出现在两个地方。
        """

        return tuple(
            StepState(
                step=event.payload.step,
                committed_event_id=event.event_id,
                sequence=event.sequence,
                decision_metadata=event.payload.decision_metadata,
                commands=tuple(
                    commands[command.command_id]
                    for command in event.payload.step.commands
                ),
            )
            for event in self.step_events
        )

    def decision_state(
        self,
        consumed_trigger_event_ids: tuple[str, ...],
    ) -> DecisionState:
        return DecisionState(
            session_id=self.session_id,
            version=self.version(),
            user_messages=tuple(self.user_messages),
            commands=tuple(self.commands.values()),
            steps=self.step_states(self.commands),
            visible_event_ids=tuple(self.visible_event_ids),
            consumed_trigger_event_ids=consumed_trigger_event_ids,
        )

    def apply_regular(self, event: Event) -> None:
        payload = event.payload
        if isinstance(payload, UserMessageReceived):
            self.user_messages.append(payload.content)
        elif isinstance(payload, CommandAuthorized):
            self._apply_authorized(event, payload)
        elif isinstance(payload, CommandRejected):
            self._apply_rejected(event, payload)
        elif isinstance(payload, DispatchAttemptStarted):
            self._apply_dispatch_started(event, payload)
        elif isinstance(payload, CommandOutcomeReceived):
            self._apply_outcome(event, payload)
        elif isinstance(payload, (DecisionCancelled, StepContinuationCancelled)):
            pass
        elif isinstance(payload, DomainFactCommitted):
            pass
        else:
            raise TypeError(f"not a regular event: {type(payload).__name__}")
        self.visible_event_ids.append(event.event_id)

    def _apply_authorized(
        self,
        event: Event,
        payload: CommandAuthorized,
    ) -> None:
        state = self.commands[payload.command_id]
        if state.authorization_rejected_by_event_id is not None:
            raise ValueError(f"command already rejected: {payload.command_id}")
        if state.dispatch_eligible_by_event_id is not None:
            raise ValueError(f"command already authorized: {payload.command_id}")
        if state.phase is not CommandPhase.PENDING:
            raise ValueError(f"command is not pending: {payload.command_id}")
        if event.causation_id != state.issued_by_event_id:
            raise ValueError(f"authorization causation mismatch: {payload.command_id}")
        self.commands[payload.command_id] = replace(
            state,
            dispatch_eligible_by_event_id=event.event_id,
        )

    def _apply_rejected(
        self,
        event: Event,
        payload: CommandRejected,
    ) -> None:
        state = self.commands[payload.command_id]
        if state.authorization_rejected_by_event_id is not None:
            raise ValueError(f"command already rejected: {payload.command_id}")
        if state.dispatch_eligible_by_event_id is not None:
            raise ValueError(f"cannot reject authorized command: {payload.command_id}")
        if state.phase is not CommandPhase.PENDING:
            raise ValueError(f"command is not pending: {payload.command_id}")
        if event.causation_id != state.issued_by_event_id:
            raise ValueError(f"rejection causation mismatch: {payload.command_id}")
        self.commands[payload.command_id] = replace(
            state,
            authorization_rejected_by_event_id=event.event_id,
        )

    def _apply_dispatch_started(
        self,
        event: Event,
        payload: DispatchAttemptStarted,
    ) -> None:
        state = self.commands[payload.command_id]
        if state.phase is not CommandPhase.PENDING:
            raise ValueError(f"command is not pending: {payload.command_id}")
        if event.causation_id != state.dispatch_eligible_by_event_id:
            raise ValueError(f"dispatch causation mismatch: {payload.attempt_id}")
        attempt_key = (payload.command_id, payload.attempt_id)
        if attempt_key in self.attempt_event_ids:
            raise ValueError(f"duplicate command attempt: {payload.attempt_id}")
        if payload.attempt_number != len(state.attempts) + 1:
            raise ValueError(f"attempt number mismatch: {payload.attempt_id}")
        attempt = AttemptState(
            attempt_id=payload.attempt_id,
            attempt_number=payload.attempt_number,
            started_event_id=event.event_id,
        )
        self.attempt_event_ids[attempt_key] = event.event_id
        self.dispatch_command_ids[event.event_id] = payload.command_id
        self.commands[payload.command_id] = replace(
            state,
            phase=CommandPhase.UNKNOWN,
            attempts=(*state.attempts, attempt),
            dispatch_eligible_by_event_id=None,
        )

    def _apply_outcome(
        self,
        event: Event,
        payload: CommandOutcomeReceived,
    ) -> None:
        state = self.commands[payload.command_id]
        attempts = state.attempts
        if payload.attempt_id is None:
            raise ValueError(f"outcome has no attempt: {payload.command_id}")
        attempt = self._attempt(state, payload.attempt_id)
        if event.causation_id != attempt.started_event_id:
            raise ValueError(f"attempt fact causation mismatch: {payload.command_id}")
        if attempt.outcome is not None:
            raise ValueError(f"attempt already terminal: {payload.attempt_id}")
        attempts = self._replace_attempt(
            state,
            replace(
                attempt,
                phase=AttemptPhase.TERMINAL,
                outcome=payload.outcome,
            ),
        )
        if state.outcome is None:
            self.canonical_outcome_event_ids.add(event.event_id)
            self.commands[payload.command_id] = replace(
                state,
                phase=CommandPhase.TERMINAL,
                attempts=attempts,
                outcome=payload.outcome,
                canonical_outcome_event_id=event.event_id,
                dispatch_eligible_by_event_id=None,
            )
        else:
            self.commands[payload.command_id] = replace(
                state,
                attempts=attempts,
            )

    @staticmethod
    def _attempt(state: CommandState, attempt_id: str) -> AttemptState:
        for attempt in state.attempts:
            if attempt.attempt_id == attempt_id:
                return attempt
        raise ValueError(
            f"unknown attempt for {state.command.command_id}: {attempt_id}"
        )

    @staticmethod
    def _replace_attempt(
        state: CommandState,
        updated: AttemptState,
    ) -> tuple[AttemptState, ...]:
        return tuple(
            updated if attempt.attempt_id == updated.attempt_id else attempt
            for attempt in state.attempts
        )

    def apply_step(
        self,
        event: Event,
        *,
        expected_basis_state_version: str | None = None,
    ) -> None:
        payload = event.payload
        if not isinstance(payload, StepCommitted):
            raise TypeError(f"not a step event: {type(payload).__name__}")
        step = payload.step
        if step.step_id in self.step_ids:
            raise ValueError(f"duplicate step id: {step.step_id}")
        if (
            expected_basis_state_version is not None
            and step.basis_state_version != expected_basis_state_version
        ):
            raise ValueError(f"step basis mismatch: {step.step_id}")
        for command in step.commands:
            if command.command_id in self.commands:
                raise ValueError(f"duplicate command id: {command.command_id}")
            self.commands[command.command_id] = CommandState(
                command=command,
                phase=CommandPhase.PENDING,
                issued_by_event_id=event.event_id,
                dispatch_eligible_by_event_id=(
                    None if command.requires_authorization else event.event_id
                ),
            )
        self.step_events.append(event)
        self.step_ids.add(step.step_id)
        self.visible_event_ids.append(event.event_id)


def _is_external_decision_fact(payload: object) -> bool:
    """External arrival that can become the next decision trigger."""

    if isinstance(payload, UserMessageReceived):
        return True
    return isinstance(payload, DomainFactCommitted) and payload.requests_decision


class StateProjector:
    def project(
        self,
        session_id: str,
        events: tuple[Event, ...],
    ) -> RuntimeProjection:
        self._validate_session(session_id, events)
        step_events = self._index_step_events(events)
        cancellation_events = self._index_cancellation_events(events)
        continuation_cancellations = self._index_continuation_cancellations(events)
        duplicate_consumptions = set(step_events) & set(cancellation_events)
        if duplicate_consumptions:
            raise ValueError(
                "decision event consumed twice: "
                f"{sorted(duplicate_consumptions)[0]}"
            )
        event_sequences = {event.event_id: event.sequence for event in events}
        issuing_observed = {
            event.event_id: event.payload.step.observed_journal_position
            for event in events
            if isinstance(event.payload, StepCommitted)
        }

        operational = _StateBuilder(session_id)
        for event in events:
            if isinstance(event.payload, StepCommitted):
                operational.apply_step(event)
            else:
                operational.apply_regular(event)

        decision = _StateBuilder(session_id)
        consumed: list[str] = []
        applied_step_event_ids: set[str] = set()
        applied_cancellation_event_ids: set[str] = set()
        next_trigger: Event | None = None

        for event in events:
            if isinstance(event.payload, StepCommitted):
                continue
            if event.event_id in decision.visible_event_ids:
                continue
            decision.apply_regular(event)
            cancellation_event = cancellation_events.get(event.event_id)
            if cancellation_event is not None:
                decision.apply_regular(cancellation_event)
                consumed.append(event.event_id)
                applied_cancellation_event_ids.add(cancellation_event.event_id)
                continue
            step_event = step_events.get(event.event_id)
            if step_event is not None:
                step = step_event.payload.step
                if _is_external_decision_fact(event.payload):
                    self._include_waited_follow_up_events(
                        decision,
                        events,
                        event,
                        operational,
                        until_sequence=step.observed_journal_position,
                    )
                expected_cursor = len(consumed) + 1
                if step.decision_cursor != expected_cursor:
                    raise ValueError(f"step decision cursor mismatch: {step.step_id}")
                minimum_observed_position = max(
                    event_sequences[event_id]
                    for event_id in decision.visible_event_ids
                )
                if not (
                    minimum_observed_position
                    <= step.observed_journal_position
                    < step_event.sequence
                ):
                    raise ValueError(
                        f"step observed position mismatch: {step.step_id}"
                    )
                decision.apply_step(
                    step_event,
                    expected_basis_state_version=decision.version(),
                )
                consumed.append(event.event_id)
                applied_step_event_ids.add(step_event.event_id)
                continue
            if not self._requires_decision(
                event,
                decision,
                events,
                issuing_observed,
                continuation_cancellations,
            ):
                continue
            if _is_external_decision_fact(event.payload) and (
                self._blocked_by_in_flight(
                    event,
                    operational,
                    issuing_observed,
                )
            ):
                break
            next_trigger = event
            if _is_external_decision_fact(event.payload):
                self._include_waited_follow_up_events(
                    decision,
                    events,
                    event,
                    operational,
                    until_sequence=events[-1].sequence,
                )
            break

        unapplied = {
            event.event_id for event in step_events.values()
        } - applied_step_event_ids
        if unapplied:
            raise ValueError(
                f"step commits cross an unconsumed decision event: {sorted(unapplied)}"
            )
        unapplied_cancellations = {
            event.event_id for event in cancellation_events.values()
        } - applied_cancellation_event_ids
        if unapplied_cancellations:
            raise ValueError(
                "cancellations cross an unconsumed decision event: "
                f"{sorted(unapplied_cancellations)}"
            )

        decision_state = decision.decision_state(tuple(consumed))
        journal_position = events[-1].sequence if events else 0
        next_frame = (
            DecisionFrame(
                trigger_event=next_trigger,
                state=decision_state,
                decision_cursor=len(consumed) + 1,
                basis_state_version=decision_state.version,
                observed_journal_position=journal_position,
            )
            if next_trigger is not None
            else None
        )
        command_states = tuple(operational.commands.values())
        unauthorized_pending = tuple(
            state.command.command_id
            for state in command_states
            if state.phase is CommandPhase.PENDING
            and state.dispatch_eligible_by_event_id is None
            and state.authorization_rejected_by_event_id is None
        )
        waiting_command_ids = tuple(
            state.command.command_id
            for state in command_states
            if state.phase is not CommandPhase.TERMINAL
            and state.authorization_rejected_by_event_id is None
        )
        waiting_for = (
            ()
            if next_frame is not None
            else (
                tuple(
                    f"authorization:{command_id}" for command_id in unauthorized_pending
                )
                + tuple(
                    f"command:{command_id}"
                    for command_id in waiting_command_ids
                    if command_id not in unauthorized_pending
                )
                or ("external_fact",)
            )
        )
        state = CanonicalState(
            session_id=session_id,
            journal_position=journal_position,
            decision_cursor=len(consumed),
            status=(
                RuntimeStatus.RUNNABLE
                if next_frame is not None
                else RuntimeStatus.WAITING
            ),
            commands=command_states,
            steps=decision.step_states(operational.commands),
            next_trigger_event_id=(
                next_trigger.event_id if next_trigger is not None else None
            ),
            waiting_command_ids=waiting_command_ids,
            waiting_for=waiting_for,
        )
        return RuntimeProjection(state=state, next_decision=next_frame)

    def project_visible(
        self,
        session_id: str,
        events: tuple[Event, ...],
        visible_event_ids: tuple[str, ...] | None = None,
    ) -> DecisionState:
        """把一个已经选定的可见集合归约成结构化视图，默认全可见。

        哪个 Step 发起了哪个 Command、Command 终局没有，是确定性的事实
        归约，归 Runtime。下游若自己从事件流重建，得到的是一份没有这些
        约束的复制品：可见集合里出现一个 Step 不可见的 Outcome 时，这里
        会当场炸，重建版本只会安静地少一条消息。
        """

        visible = None if visible_event_ids is None else set(visible_event_ids)
        builder = _StateBuilder(session_id)
        for event in events:
            if visible is not None and event.event_id not in visible:
                continue
            if isinstance(event.payload, StepCommitted):
                builder.apply_step(event)
            else:
                builder.apply_regular(event)
        return builder.decision_state(())

    @staticmethod
    def _validate_session(session_id: str, events: tuple[Event, ...]) -> None:
        event_ids: set[str] = set()
        previous_sequence = 0
        for event in events:
            if event.session_id != session_id:
                raise ValueError(f"event belongs to another session: {event.event_id}")
            if event.sequence <= previous_sequence:
                raise ValueError(f"invalid event sequence: {event.event_id}")
            previous_sequence = event.sequence
            if event.schema_version != EVENT_SCHEMA_VERSION:
                raise ValueError(
                    f"unsupported event schema version: {event.schema_version}"
                )
            if event.event_id in event_ids:
                raise ValueError(f"duplicate event id: {event.event_id}")
            event_ids.add(event.event_id)

    @staticmethod
    def _index_step_events(events: tuple[Event, ...]) -> dict[str, Event]:
        events_by_id = {event.event_id: event for event in events}
        result: dict[str, Event] = {}
        for event in events:
            payload = event.payload
            if not isinstance(payload, StepCommitted):
                continue
            trigger_id = payload.step.trigger_event_id
            trigger = events_by_id.get(trigger_id)
            if trigger is None:
                raise ValueError(f"step trigger does not exist: {trigger_id}")
            if trigger.sequence >= event.sequence:
                raise ValueError(f"step precedes its trigger: {payload.step.step_id}")
            if event.causation_id != trigger_id:
                raise ValueError(f"step causation mismatch: {payload.step.step_id}")
            if trigger_id in result:
                raise ValueError(f"decision event consumed twice: {trigger_id}")
            result[trigger_id] = event
        return result

    @staticmethod
    def _index_cancellation_events(events: tuple[Event, ...]) -> dict[str, Event]:
        events_by_id = {event.event_id: event for event in events}
        result: dict[str, Event] = {}
        for event in events:
            payload = event.payload
            if not isinstance(payload, DecisionCancelled):
                continue
            trigger_id = payload.trigger_event_id
            trigger = events_by_id.get(trigger_id)
            if trigger is None:
                raise ValueError(
                    f"cancelled decision trigger does not exist: {trigger_id}"
                )
            if trigger.sequence >= event.sequence:
                raise ValueError(
                    f"decision cancellation precedes its trigger: {event.event_id}"
                )
            if event.causation_id != trigger_id:
                raise ValueError(
                    f"decision cancellation causation mismatch: {event.event_id}"
                )
            if trigger_id in result:
                raise ValueError(f"decision event consumed twice: {trigger_id}")
            result[trigger_id] = event
        return result

    @staticmethod
    def _index_continuation_cancellations(
        events: tuple[Event, ...],
    ) -> dict[str, Event]:
        events_by_id = {event.event_id: event for event in events}
        result: dict[str, Event] = {}
        for event in events:
            payload = event.payload
            if not isinstance(payload, StepContinuationCancelled):
                continue
            step_event = events_by_id.get(payload.step_event_id)
            if step_event is None:
                raise ValueError(
                    f"cancelled continuation step does not exist: {payload.step_event_id}"
                )
            if not isinstance(step_event.payload, StepCommitted):
                raise ValueError(
                    f"cancelled continuation target is not a step: {payload.step_event_id}"
                )
            if step_event.sequence >= event.sequence:
                raise ValueError(
                    f"continuation cancellation precedes its step: {event.event_id}"
                )
            if event.causation_id != payload.step_event_id:
                raise ValueError(
                    f"continuation cancellation causation mismatch: {event.event_id}"
                )
            if payload.step_event_id in result:
                raise ValueError(
                    f"step continuation cancelled twice: {payload.step_event_id}"
                )
            result[payload.step_event_id] = event
        return result

    @staticmethod
    def _requires_decision(
        event: Event,
        state: _StateBuilder,
        events: tuple[Event, ...],
        issuing_observed: dict[str, int],
        continuation_cancellations: dict[str, Event],
    ) -> bool:
        payload = event.payload
        if _is_external_decision_fact(payload):
            return True
        if isinstance(payload, CommandRejected):
            return True
        if isinstance(payload, CommandOutcomeReceived):
            command_state = state.commands[payload.command_id]
            if not (
                event.event_id in state.canonical_outcome_event_ids
                and command_state.command.decision_on_outcome
                and command_state.issued_by_event_id
                not in continuation_cancellations
                and _parallel_decision_group_closed(state, command_state)
            ):
                return False
            observed = issuing_observed[command_state.issued_by_event_id]
            if any(
                _is_external_decision_fact(item.payload)
                and item.sequence > observed
                for item in events
            ):
                return False
            return True
        return False

    @staticmethod
    def _blocked_by_in_flight(
        message: Event,
        operational: _StateBuilder,
        issuing_observed: dict[str, int],
    ) -> bool:
        """Whether this external fact must wait for Commands that precede it.

        Deliberately asymmetric with `_is_waited_follow_up`. This asks
        whether the trigger should wait, so it compares journal positions:
        a Step committed after the fact arrived is not that fact's business.
        That one asks what a frozen frame must show, so it follows the
        decision's causal order: the same Step, once visible, still has to
        carry its own Command outcomes. Unifying them would deadlock this
        fact behind a Step it never waited for.
        """

        for command_state in operational.commands.values():
            if (
                issuing_observed[command_state.issued_by_event_id]
                >= message.sequence
            ):
                continue
            if not command_state.attempts:
                continue
            if command_state.phase is CommandPhase.TERMINAL:
                continue
            return True
        return False

    @staticmethod
    def _include_waited_follow_up_events(
        decision: _StateBuilder,
        events: tuple[Event, ...],
        trigger: Event,
        operational: _StateBuilder,
        until_sequence: int,
    ) -> None:
        past_trigger = False
        for event in events:
            if event.event_id == trigger.event_id:
                past_trigger = True
                continue
            if not past_trigger:
                continue
            if event.sequence > until_sequence:
                return
            if event.event_id in decision.visible_event_ids:
                continue
            if not _is_waited_follow_up(event, decision):
                continue
            decision.apply_regular(event)
            if _waited_batch_complete(decision, operational):
                return


def _is_waited_follow_up(
    event: Event,
    decision: _StateBuilder,
) -> bool:
    """Whether this fact closes a Command the decision state can already see.

    Membership follows the decision's causal order, not journal position.
    A Step that consumed an earlier trigger is visible here even when it
    committed after this trigger arrived; its Commands must close with it.
    """

    payload = event.payload
    if isinstance(payload, (DispatchAttemptStarted, CommandOutcomeReceived)):
        return payload.command_id in decision.commands
    return False


def _waited_batch_complete(
    decision: _StateBuilder,
    operational: _StateBuilder,
) -> bool:
    for command_id, frozen in decision.commands.items():
        if not operational.commands[command_id].attempts:
            continue
        if frozen.phase is not CommandPhase.TERMINAL:
            return False
    return True


def _parallel_decision_group_closed(
    state: _StateBuilder,
    command_state: CommandState,
) -> bool:
    """Return whether an unordered parallel command group is terminal.

    Commands issued by one Step with ``decision_on_outcome=True`` form a
    set.  Their start and completion order is journal evidence only; it
    must not create intermediate model decisions.
    """
    issued_by = command_state.issued_by_event_id
    for sibling in state.commands.values():
        if sibling.issued_by_event_id != issued_by:
            continue
        if not sibling.command.decision_on_outcome:
            continue
        if sibling.phase is not CommandPhase.TERMINAL:
            return False
    return True
