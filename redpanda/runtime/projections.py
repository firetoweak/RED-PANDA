from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from redpanda.runtime.events import Event, UserMessageReceived
from redpanda.runtime.model import CanonicalState
from redpanda.runtime.state import StateProjector


@dataclass(frozen=True, slots=True)
class TraceEntry:
    sequence: int
    event_id: str
    kind: str
    causation_id: str | None


@dataclass(frozen=True, slots=True)
class TraceView:
    session_id: str
    entries: tuple[TraceEntry, ...]


@dataclass(frozen=True, slots=True)
class ArtifactResolution:
    refs: tuple[str, ...]
    missing: tuple[str, ...]
    inspected: bool = True

    @property
    def complete(self) -> bool:
        if not self.inspected:
            return not self.refs
        return not self.missing


@dataclass(frozen=True, slots=True)
class ReplayView:
    state: CanonicalState
    trace: TraceView
    artifacts: ArtifactResolution


def turn_notice_current(events: tuple[Event, ...], sequence: int) -> bool:
    """这条事实之后用户没有再提交消息，它产生的提示仍属于当前轮次。

    提示跟着轮次走：用户提交下一条消息就开始新一轮，上一轮的结果不再
    呈现。事实本身留在 journal 里，过期只影响人看到的提示。
    """
    return not any(
        isinstance(event.payload, UserMessageReceived) and event.sequence > sequence
        for event in events
    )


def diagnose_artifacts(
    events: tuple[Event, ...],
    available_refs: Collection[str] | None = None,
) -> ArtifactResolution:
    refs = tuple(dict.fromkeys(ref for event in events for ref in event.artifact_refs))
    if available_refs is None:
        return ArtifactResolution(refs=refs, missing=(), inspected=False)
    available = frozenset(available_refs)
    missing = tuple(ref for ref in refs if ref not in available)
    return ArtifactResolution(refs=refs, missing=missing, inspected=True)


def project_trace(
    session_id: str,
    events: tuple[Event, ...],
) -> TraceView:
    return TraceView(
        session_id=session_id,
        entries=tuple(
            TraceEntry(
                sequence=event.sequence,
                event_id=event.event_id,
                kind=event.kind,
                causation_id=event.causation_id,
            )
            for event in events
        ),
    )


def replay(
    session_id: str,
    events: tuple[Event, ...],
    available_artifact_refs: Collection[str] | None = None,
) -> ReplayView:
    projector = StateProjector()
    return ReplayView(
        state=projector.project(session_id, events).state,
        trace=project_trace(session_id, events),
        artifacts=diagnose_artifacts(events, available_artifact_refs),
    )
