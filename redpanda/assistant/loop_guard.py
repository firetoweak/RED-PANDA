"""Mechanical observations; consumed exclusively by the independent progress watch."""

from redpanda.assistant.loop_guard_strategies import ConsecutiveActions
from redpanda.assistant.system_messages import system_message


SOURCE = "progress_check"


class LoopGuard:
    def __init__(self, strategies=None):
        self.strategies = (ConsecutiveActions(),) if strategies is None else tuple(strategies)

    def inspect(self, events, position):
        covered = 0
        events = tuple(event for event in events if event.sequence <= position)
        for event in events:
            notice = system_message(event)
            if notice is not None and event.delivery is not None and event.delivery.source == SOURCE:
                covered = next(e.sequence for e in events if e.event_id == event.causation_id)

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
        return {
            "covered_after": covered,
            "covered_through": position,
            "evidence": evidence,
        }
