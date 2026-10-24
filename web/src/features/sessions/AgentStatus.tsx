import { Tooltip } from "@mantine/core";
import { IconAlertCircle, IconMessageCircle } from "@tabler/icons-react";
import type { AgentRosterEntry } from "../../api/contracts";
import { useAppSelector } from "../../app/hooks";

const STATUS_LABEL = { working: "干活", authorization: "等授权", waiting: "等人回话", idle: "空闲" } as const;

export function AgentStatus({ agent, attention = false }: { agent: AgentRosterEntry; attention?: boolean }) {
  const live = useAppSelector((state) => state.runtime.sessions[agent.session_id]?.activity);
  const status = agent.status === "authorization" ? "authorization"
    : live === "running" ? "working" : agent.status;
  const label = `${STATUS_LABEL[status]}${attention ? " · 需要你处理" : ""}`;
  const needsAttention = attention || status === "authorization";
  const dot = needsAttention ? "attention" : status === "working" ? "running" : status === "waiting" ? "waiting" : "idle";
  return (
    <Tooltip label={label}>
      <span className="agent-status" data-status={status}>
        <span className={`activity-dot activity-dot-${dot}`} role="img" aria-label={label}>
          {needsAttention ? <IconAlertCircle aria-hidden="true" size={13} /> : status === "waiting" ? <IconMessageCircle aria-hidden="true" size={13} /> : null}
        </span>
      </span>
    </Tooltip>
  );
}
