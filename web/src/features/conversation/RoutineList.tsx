import { Badge, Button, Group, Switch, Text } from "@mantine/core";
import { useState } from "react";

import type { Routine, RoutineFire } from "../../api/contracts";
import {
  useGetConversationQuery,
  useGetRoutineFiresQuery,
  useGetRoutinesQuery,
  useSetRoutineStateMutation,
} from "../../api/redpandaApi";
import { formatSchedule } from "./formatSchedule";
import { formatMessageTime } from "./messageTime";
import { focusTimeline } from "./timelineFocus";

const STATE_LABEL: Record<Routine["state"], string> = {
  active: "运行中",
  paused: "Agent 暂停",
  paused_by_user: "人暂停",
  paused_unwatched: "无人查看已暂停",
};

/** Agent 资料里的例行任务。人可以暂停、恢复或删除；人暂停的只有人能恢复。 */
export function RoutineList({ sessionId, connectionId }: { sessionId: string; connectionId: string | null }) {
  const routines = useGetRoutinesQuery(sessionId).currentData ?? [];
  const [openedId, setOpenedId] = useState<string | null>(null);
  const opened = routines.find((item) => item.routine_id === openedId) ?? null;
  return (
    <div className="agent-details-section">
      {opened === null ? (
        <>
          <Text className="agent-details-label" size="xs" c="dimmed">例行任务</Text>
          {routines.length === 0 ? <Text size="sm" c="dimmed">无例行任务</Text> : (
            <div className="routine-list">
              {routines.map((routine) => (
                <RoutineRow
                  key={routine.routine_id}
                  routine={routine}
                  sessionId={sessionId}
                  connectionId={connectionId}
                  onOpen={() => setOpenedId(routine.routine_id)}
                />
              ))}
            </div>
          )}
        </>
      ) : (
        <RoutineDetail
          routine={opened}
          sessionId={sessionId}
          connectionId={connectionId}
          onBack={() => setOpenedId(null)}
        />
      )}
    </div>
  );
}

function whenLine(routine: Routine): string {
  const last = routine.last_run === null ? "尚未运行" : formatMessageTime(routine.last_run).short;
  const next = routine.next_run === null ? "未安排" : formatMessageTime(routine.next_run).short;
  return `上次 ${last} · 下次 ${next}`;
}

function useRoutineControls(sessionId: string, connectionId: string | null, routine: Routine) {
  const [setState, request] = useSetRoutineStateMutation();
  const [confirming, setConfirming] = useState(false);
  const disabled = connectionId === null || request.isLoading;
  const failure: unknown = request.error;
  const change = (state: "active" | "paused_by_user" | "deleted") => {
    if (connectionId === null) return;
    void setState({ connectionId, sessionId, routineId: routine.routine_id, state });
  };
  return { confirming, setConfirming, disabled, change, failure };
}

function PauseSwitch({ routine, disabled, onChange }: {
  routine: Routine;
  disabled: boolean;
  onChange: (state: "active" | "paused_by_user") => void;
}) {
  const on = routine.state === "active";
  return (
    <span className="routine-switch">
      <Switch
        aria-label={on ? `暂停 ${routine.name}` : `恢复 ${routine.name}`}
        checked={on}
        disabled={disabled}
        onChange={(event) => onChange(event.currentTarget.checked ? "active" : "paused_by_user")}
        size="xs"
      />
    </span>
  );
}

function DeleteControl({ routine, disabled, confirming, onConfirm, onAsk, onKeep }: {
  routine: Routine;
  disabled: boolean;
  confirming: boolean;
  onConfirm: () => void;
  onAsk: () => void;
  onKeep: () => void;
}) {
  return confirming ? (
    <Group gap={4} wrap="nowrap">
      <Button aria-label={`确认删除 ${routine.name}`} color="red" disabled={disabled}
        onClick={onConfirm} size="compact-xs">
        确认删除
      </Button>
      <Button onClick={onKeep} size="compact-xs" variant="subtle">保留</Button>
    </Group>
  ) : (
    <Button aria-label={`删除 ${routine.name}`} color="red" disabled={disabled}
      onClick={onAsk} size="compact-xs" variant="subtle">
      删除
    </Button>
  );
}

function FailureText({ failure }: { failure: unknown }) {
  return failure === undefined ? null : (
    <Text c="red" fz={11}>
      {typeof failure === "string" ? failure : "操作失败，请确认后端连接后重试。"}
    </Text>
  );
}

function RoutineRow({ routine, sessionId, connectionId, onOpen }: {
  routine: Routine;
  sessionId: string;
  connectionId: string | null;
  onOpen: () => void;
}) {
  const controls = useRoutineControls(sessionId, connectionId, routine);
  return (
    <div className="routine-row">
      <PauseSwitch routine={routine} disabled={controls.disabled}
        onChange={controls.change} />
      <button type="button" className="routine-open" aria-label={`查看 ${routine.name}`} onClick={onOpen}>
        <span className="routine-name-line">
          <Text className="routine-name" component="span" size="sm" fw={500} truncate>{routine.name}</Text>
          <Badge color={routine.state === "active" ? "ember" : "gray"} size="xs" variant="light">
            {STATE_LABEL[routine.state]}
          </Badge>
        </span>
        <Text component="span" size="xs">{formatSchedule(routine.schedule)}</Text>
        <Text component="span" c="dimmed" fz={11}>{whenLine(routine)}</Text>
      </button>
      <div className="routine-toolbar">
        <DeleteControl
          routine={routine}
          disabled={controls.disabled}
          confirming={controls.confirming}
          onAsk={() => controls.setConfirming(true)}
          onKeep={() => controls.setConfirming(false)}
          onConfirm={() => controls.change("deleted")}
        />
      </div>
      <FailureText failure={controls.failure} />
    </div>
  );
}

function RoutineDetail({ routine, sessionId, connectionId, onBack }: {
  routine: Routine;
  sessionId: string;
  connectionId: string | null;
  onBack: () => void;
}) {
  const controls = useRoutineControls(sessionId, connectionId, routine);
  const fires = useGetRoutineFiresQuery({ sessionId, routineId: routine.routine_id });
  const conversation = useGetConversationQuery(sessionId);
  const plan = formatSchedule(routine.schedule);
  const outputs = new Set(
    (conversation.currentData?.items ?? [])
      .filter((item) => item.kind === "step")
      .map((item) => item.output_id),
  );
  const records = fires.currentData;
  return (
    <div className="routine-detail">
      <Button className="routine-back" onClick={onBack} size="compact-xs" variant="subtle">返回例行任务</Button>
      <Group justify="space-between" wrap="nowrap" gap="xs">
        <Text fw={600} size="sm" truncate>{routine.name}</Text>
        <PauseSwitch routine={routine} disabled={controls.disabled} onChange={controls.change} />
      </Group>
      <Badge color={routine.state === "active" ? "ember" : "gray"} size="xs" variant="light" w="fit-content">
        {STATE_LABEL[routine.state]}
      </Badge>
      <Text size="sm">{plan}</Text>
      {plan === routine.schedule ? null : (
        <Text c="dimmed" ff="monospace" fz={11}>{routine.schedule}</Text>
      )}
      <Text c="dimmed" fz={11}>{whenLine(routine)}</Text>
      <DeleteControl
        routine={routine}
        disabled={controls.disabled}
        confirming={controls.confirming}
        onAsk={() => controls.setConfirming(true)}
        onKeep={() => controls.setConfirming(false)}
        onConfirm={() => controls.change("deleted")}
      />
      <FailureText failure={controls.failure} />
      <Text className="agent-details-label" size="xs" c="dimmed">意图</Text>
      <Text className="routine-intent" size="sm">{routine.intent}</Text>
      <Text className="agent-details-label" size="xs" c="dimmed">执行记录</Text>
      {fires.isError ? <Text c="red" fz={11}>执行记录加载失败</Text>
        : records === undefined ? null
        : records.length === 0 ? <Text size="sm" c="dimmed">还没有执行过</Text>
        : records.map((fire) => (
          <FireRow key={fire.fire_id} fire={fire} linked={fire.output_id !== null && outputs.has(fire.output_id)} />
        ))}
    </div>
  );
}

function FireRow({ fire, linked }: { fire: RoutineFire; linked: boolean }) {
  const planned = formatMessageTime(fire.planned_at);
  const fired = formatMessageTime(fire.fired_at);
  const delivered = fire.delivered_at !== null;
  const outputId = linked ? fire.output_id : null;
  return (
    <div className="routine-fire">
      <Text size="xs">计划 {planned.short} · 触发 {fired.short}</Text>
      <Text size="xs" c={delivered ? undefined : "dimmed"}>{delivered ? "已送达" : "未送达"}</Text>
      {outputId === null ? null : (
        <Button
          aria-label={`在时间线中查看 ${fired.short}`}
          onClick={() => focusTimeline(outputId)}
          size="compact-xs"
          variant="subtle"
        >
          在时间线中查看
        </Button>
      )}
    </div>
  );
}
