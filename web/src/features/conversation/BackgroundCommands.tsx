import { Badge, Button, Group, Paper, Stack, Text, UnstyledButton } from "@mantine/core";
import { useState } from "react";
import type { BackgroundCommand } from "../../api/contracts";
import { useGetBackgroundCommandsQuery, useStopBackgroundCommandMutation } from "../../api/redpandaApi";

const LABEL = { running: "运行中", completed: "已结束", stopped: "已停止", unknown: "结果未知" };

export function BackgroundCommands({ sessionId, connectionId }: { sessionId: string; connectionId: string | null }) {
  const query = useGetBackgroundCommandsQuery(sessionId, { pollingInterval: 2000, skipPollingIfUnfocused: true });
  const [opened, setOpened] = useState(false);
  const commands = query.currentData ?? [];
  if (query.isError) return <Text c="red" size="xs">后台命令状态读取失败</Text>;
  if (commands.length === 0) return null;
  const running = commands.filter((command) => command.state === "running").length;
  return (
    <Paper className="routine-list" radius="md" withBorder>
      <UnstyledButton className="routine-list-toggle" aria-expanded={opened} onClick={() => setOpened(!opened)}>
        <Text size="sm" fw={600}>后台命令 · {running} 项运行中{opened ? " ▾" : " ▸"}</Text>
      </UnstyledButton>
      {opened ? <Stack className="routine-list-body" gap="xs">
        {commands.map((command) => <CommandRow key={command.command_id} command={command}
          sessionId={sessionId} connectionId={connectionId} refresh={() => { void query.refetch(); }} />)}
      </Stack> : null}
    </Paper>
  );
}

function CommandRow({ command, sessionId, connectionId, refresh }: {
  command: BackgroundCommand; sessionId: string; connectionId: string | null; refresh: () => void;
}) {
  const [stop, request] = useStopBackgroundCommandMutation();
  const seconds = Math.floor(command.duration_ms / 1000);
  return <Stack gap={3}>
    <Group justify="space-between" wrap="nowrap">
      <Badge size="xs" color={command.state === "running" ? "ember" : "gray"}>{LABEL[command.state]}</Badge>
      <Text size="xs" c="dimmed">{seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`}
        {command.exit_code === null ? "" : ` · 退出码 ${command.exit_code}`}</Text>
      {command.state === "running" ? <Button size="compact-xs" color="red" variant="subtle"
        disabled={connectionId === null} loading={request.isLoading} onClick={() => {
          if (connectionId !== null) void stop({ sessionId, connectionId, commandId: command.command_id })
            .then(refresh);
        }}>停止命令</Button> : null}
    </Group>
    <Text size="xs" ff="monospace" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{command.command}</Text>
    {request.isError ? <Text size="xs" c="red">停止失败，请重试。</Text> : null}
  </Stack>;
}
