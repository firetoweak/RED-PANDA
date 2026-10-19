import { Badge, Button, Group, Paper, Stack, Text, UnstyledButton } from "@mantine/core";
import { IconChevronDown, IconChevronRight, IconRepeat } from "@tabler/icons-react";
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

/** 这个 Agent 的例行任务。人可以暂停、恢复或取消；人暂停的只有人能恢复。 */
export function RoutineList({ sessionId, connectionId }: { sessionId: string; connectionId: string | null }) {
  const routines = useGetRoutinesQuery(sessionId).currentData ?? [];
  const [opened, setOpened] = useState(false);
  if (routines.length === 0) {
    return null;
  }
  const paused = routines.filter((routine) => routine.state !== "active").length;
  return (
    <Paper className="routine-list" radius="md" withBorder>
      <UnstyledButton aria-expanded={opened} className="routine-list-toggle" onClick={() => setOpened((value) => !value)}>
        <Group gap={8} wrap="nowrap">
          {opened ? <IconChevronDown size={15} /> : <IconChevronRight size={15} />}
          <IconRepeat size={15} />
          <Text fw={600} size="sm">例行任务</Text>
          <Text c="dimmed" fz={11}>
            {routines.length} 项{paused === 0 ? "" : ` · ${paused} 项暂停`}
          </Text>
        </Group>
      </UnstyledButton>
      {opened ? (
        <Stack className="routine-list-body" gap={6}>
          {routines.map((routine) => (
            <RoutineRow key={routine.routine_id} routine={routine} sessionId={sessionId} connectionId={connectionId} />
          ))}
        </Stack>
      ) : null}
    </Paper>
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
