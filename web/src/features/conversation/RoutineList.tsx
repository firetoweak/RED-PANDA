import { Badge, Button, Group, Stack, Text } from "@mantine/core";
import { useState } from "react";

import type { Routine } from "../../api/contracts";
import { useGetRoutinesQuery, useSetRoutineStateMutation } from "../../api/redpandaApi";
import { formatMessageTime } from "./messageTime";

const STATE_LABEL: Record<Routine["state"], string> = {
  active: "运行中",
  paused: "Agent 暂停",
  paused_by_user: "人暂停",
  paused_unwatched: "无人查看已暂停",
};

/** Agent 资料里的例行任务。人可以暂停、恢复或取消；人暂停的只有人能恢复。 */
export function RoutineList({ sessionId, connectionId }: { sessionId: string; connectionId: string | null }) {
  const routines = useGetRoutinesQuery(sessionId).currentData ?? [];
  return (
    <div className="agent-details-section">
      <Text className="agent-details-label" size="xs" c="dimmed">例行任务</Text>
      {routines.length === 0 ? <Text size="sm" c="dimmed">无例行任务</Text> : (
        <Stack gap={10}>
          {routines.map((routine) => (
            <RoutineRow key={routine.routine_id} routine={routine} sessionId={sessionId} connectionId={connectionId} />
          ))}
        </Stack>
      )}
    </div>
  );
}

function RoutineRow({ routine, sessionId, connectionId }: {
  routine: Routine;
  sessionId: string;
  connectionId: string | null;
}) {
  const [setState, request] = useSetRoutineStateMutation();
  const [confirming, setConfirming] = useState(false);
  const next = routine.next_run === null ? null : formatMessageTime(routine.next_run);
  const disabled = connectionId === null || request.isLoading;
  const failure: unknown = request.error;
  const change = (state: "active" | "paused_by_user" | "deleted") => {
    if (connectionId === null) return;
    void setState({ connectionId, sessionId, routineId: routine.routine_id, state });
  };
  return (
    <Stack className="routine-row" gap={2}>
      <Group justify="space-between" gap="xs" wrap="nowrap">
        <Group gap={8} wrap="nowrap" style={{ minWidth: 0 }}>
          <Text size="sm" fw={500} truncate title={routine.intent}>{routine.name}</Text>
          <Badge color={routine.state === "active" ? "ember" : "gray"} size="xs" variant="light">
            {STATE_LABEL[routine.state]}
          </Badge>
        </Group>
        <Group gap={4} wrap="nowrap">
          {confirming ? (
            <>
              <Button aria-label={`确认取消 ${routine.name}`} color="red" disabled={disabled}
                onClick={() => change("deleted")} size="compact-xs">
                确认取消
              </Button>
              <Button onClick={() => setConfirming(false)} size="compact-xs" variant="subtle">保留</Button>
            </>
          ) : (
            <>
              {routine.state === "active" ? (
                <Button aria-label={`暂停 ${routine.name}`} disabled={disabled}
                  onClick={() => change("paused_by_user")} size="compact-xs" variant="default">
                  暂停
                </Button>
              ) : (
                <Button aria-label={`恢复 ${routine.name}`} disabled={disabled}
                  onClick={() => change("active")} size="compact-xs" variant="default">
                  恢复
                </Button>
              )}
              <Button aria-label={`取消 ${routine.name}`} color="red" disabled={disabled}
                onClick={() => setConfirming(true)} size="compact-xs" variant="subtle">
                取消
              </Button>
            </>
          )}
        </Group>
      </Group>
      <Text c="dimmed" ff="monospace" fz={11}>
        {routine.schedule}
        {next === null ? "" : ` · 下一次 ${next.short}`}
      </Text>
      {failure === undefined ? null : (
        <Text c="red" fz={11}>
          {typeof failure === "string" ? failure : "操作失败，请确认后端连接后重试。"}
        </Text>
      )}
    </Stack>
  );
}
