import { IconAlertCircle } from "@tabler/icons-react";
import type { AgentRosterEntry } from "../../api/contracts";
import { useAppSelector } from "../../app/hooks";

const STATUS_LABEL = { working: "干活", authorization: "等授权", waiting: "等人回话", idle: "空闲" } as const;

export function AgentStatus({ agent, compact = false }: { agent: AgentRosterEntry; compact?: boolean }) {
  const live = useAppSelector((state) => state.runtime.sessions[agent.session_id]?.activity);
  const status = agent.status === "authorization" ? "authorization"
    : live === "running" ? "working" : agent.status;
  const label = STATUS_LABEL[status];
  const dot = status === "authorization" ? "attention" : status === "working" ? "running" : "idle";
  return (
    <span className="agent-status" data-status={status} title={label}>
      <span className={`activity-dot activity-dot-${dot}`} aria-label={label}>
        {status === "authorization" ? <IconAlertCircle size={13} /> : null}
      </span>
      {compact ? null : <span aria-hidden="true">{label}</span>}
    </span>
  );
}
