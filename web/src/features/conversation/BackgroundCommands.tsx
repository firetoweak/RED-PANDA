import { Badge, Button, Group, Stack, Text } from "@mantine/core";
import type { BackgroundCommand } from "../../api/contracts";
import { useGetBackgroundCommandsQuery, useStopBackgroundCommandMutation } from "../../api/kondenApi";

export function BackgroundCommands({ sessionId, connectionId }: { sessionId: string; connectionId: string | null }) {
  const query = useGetBackgroundCommandsQuery(sessionId, { pollingInterval: 2000, skipPollingIfUnfocused: true });
  const commands = (query.currentData ?? []).filter((command) => command.state === "running");
  return (
    <section className="agent-details-section" aria-label="后台任务">
      <Group className="agent-details-label" justify="space-between" gap={6}>
        <Text size="xs" c="dimmed">后台任务</Text>
        {commands.length === 0 ? null : <Text size="xs" c="dimmed">{commands.length} 项运行中</Text>}
      </Group>
      {query.isError ? <Text role="alert" c="red" size="xs">后台任务读取失败</Text>
        : query.isLoading ? <Text size="sm" c="dimmed">正在读取…</Text>
        : commands.length === 0 ? <Text size="sm" c="dimmed">无运行中的后台任务</Text>
        : <Stack gap={0}>
        {commands.map((command) => <CommandRow key={command.command_id} command={command}
          sessionId={sessionId} connectionId={connectionId} refresh={() => { void query.refetch(); }} />)}
      </Stack>}
    </section>
  );
}

function CommandRow({ command, sessionId, connectionId, refresh }: {
  command: BackgroundCommand; sessionId: string; connectionId: string | null; refresh: () => void;
}) {
  const [stop, request] = useStopBackgroundCommandMutation();
  const seconds = Math.floor(command.duration_ms / 1000);
  return <Stack className="background-command-row" gap={4}>
    <Group justify="space-between" wrap="nowrap">
      <Badge size="xs" color="ember">运行中</Badge>
      <Button size="compact-xs" color="red" variant="subtle"
        disabled={connectionId === null} loading={request.isLoading} onClick={() => {
          if (connectionId !== null) void stop({ sessionId, connectionId, commandId: command.command_id })
            .then(refresh);
        }}>停止命令</Button>
    </Group>
    <Text size="xs" ff="monospace" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{command.command}</Text>
    <Text size="xs" c="dimmed">{seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`}</Text>
    {request.isError ? <Text size="xs" c="red">停止失败，请重试。</Text> : null}
  </Stack>;
}
