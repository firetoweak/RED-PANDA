"""Session-local notices derived exclusively from committed Journal facts."""

import json

from redpanda.assistant.loop_guard_strategies import ConsecutiveActions
from redpanda.runtime import StepCommitted


NOTICE = "loop_guard_notice"


def committed_notice(payload):
    metadata = payload.decision_metadata
    return None if metadata is None else metadata.get(NOTICE)


class LoopGuard:
    def __init__(self, strategies=None):
        self.strategies = (ConsecutiveActions(),) if strategies is None else tuple(strategies)

    def inspect(self, events, position):
        covered = 0
        events = tuple(event for event in events if event.sequence <= position)
        for event in events:
            if not isinstance(event.payload, StepCommitted):
                continue
            notice = committed_notice(event.payload)
            if notice is not None:
                covered = notice["covered_through"]

        evidence = []
        for strategy in self.strategies:
            for hit in strategy(events):
                fresh = tuple(behavior for behavior in hit.behaviors if behavior.sequence > covered)
                if len(fresh) < hit.threshold:
                    continue
                evidence.append({
                    "strategy": hit.strategy,
                    "version": hit.version,
                    "threshold": hit.threshold,
                    "observation": hit.observation,
                    "references": [
                        {"sequence": behavior.sequence, "command_id": behavior.command_id}
                        for behavior in hit.behaviors
                    ],
                    "new_count": len(fresh),
                })
        if not evidence:
            return None
        text = (
            "<loop_guard_notice>\n"
            + "\n".join(
                "系统观察（非用户指令）：" + hit["observation"] + " 调用 id："
                + json.dumps([ref["command_id"] for ref in hit["references"]], ensure_ascii=False)
                for hit in evidence
            )
            + "\n确认这些调用是否在推进目标，再决定下一步。\n</loop_guard_notice>"
        )
        return {
            "text": text,
            "covered_after": covered,
            "covered_through": position,
            "evidence": evidence,
        }
