import { Button, Group, Paper, Stack, Text, UnstyledButton } from "@mantine/core";
import { IconCheck, IconX } from "@tabler/icons-react";
import { useState } from "react";
import { Link } from "react-router-dom";

import type { InboxItem } from "../../api/contracts";
import { useAuthorizeCommandMutation, useGetInboxQuery, useResolveControlMutation } from "../../api/redpandaApi";
import { useAppSelector } from "../../app/hooks";

function argumentSummary(args: Record<string, unknown>): string | null {
  const path = args.path;
  if (typeof path === "string") {
    return path;
  }
  const first = Object.values(args).find((value) => typeof value === "string");
  return typeof first === "string" ? first.split("\n")[0] : null;
}

function InboxRow({ item, disabled, onAuthorize, onControl }: {
  item: InboxItem;
  disabled: boolean;
  onAuthorize: (sessionId: string, commandId: string, approved: boolean) => void;
  onControl: (sessionId: string, requestId: string, approved: boolean) => void;
}) {
  const [opened, setOpened] = useState(false);
  return (
    <Paper p="sm" radius="md" withBorder>
      <Stack gap="xs">
        <Text component={Link} fw={600} size="sm" to={`/sessions/${encodeURIComponent(item.session_id)}`}>
          {item.label}
        </Text>
        {item.kind === "authorization" ? (
          <Stack gap={4}>
            <Group justify="space-between" wrap="nowrap" gap="xs">
              <UnstyledButton aria-expanded={opened} aria-label={`查看 ${item.name} 的参数`}
                onClick={() => setOpened((value) => !value)} style={{ minWidth: 0 }}>
                <Text ff="monospace" fz={13} truncate>
                  {item.name}{argumentSummary(item.arguments) === null ? "" : ` · ${argumentSummary(item.arguments)}`}
                </Text>
              </UnstyledButton>
              <Group gap="xs" wrap="nowrap">
                <Button aria-label={`允许 ${item.name}`} color="ember" disabled={disabled}
                  leftSection={<IconCheck size={14} />}
                  onClick={() => onAuthorize(item.session_id, item.command_id, true)}
                  size="compact-sm" variant="light">
                  允许
                </Button>
                <Button aria-label={`拒绝 ${item.name}`} color="gray" disabled={disabled}
                  leftSection={<IconX size={14} />}
                  onClick={() => onAuthorize(item.session_id, item.command_id, false)}
                  size="compact-sm" variant="light">
                  拒绝
                </Button>
              </Group>
            </Group>
            {opened ? (
              <Text c="dimmed" className="pre-wrap" ff="monospace" fz={11}>
                {JSON.stringify(item.arguments, null, 2)}
              </Text>
            ) : null}
          </Stack>
        ) : (
          <Stack gap={4}>
            <Text className="pre-wrap" fz={13}>{item.summary}</Text>
            <Text c="dimmed" className="pre-wrap" fz={12}>风险：{item.risk}</Text>
            <Group justify="flex-end" gap="xs">
              <Button color="gray" disabled={disabled} leftSection={<IconX size={14} />}
                onClick={() => onControl(item.session_id, item.request_id, false)} size="compact-sm" variant="light">
                取消
              </Button>
              <Button color="ember" disabled={disabled} leftSection={<IconCheck size={14} />}
                onClick={() => onControl(item.session_id, item.request_id, true)} size="compact-sm" variant="light">
                确认
              </Button>
            </Group>
          </Stack>
        )}
      </Stack>
    </Paper>
  );
}

export function InboxPage() {
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const { data: items = [] } = useGetInboxQuery();
  const [authorizeCommand, authorizing] = useAuthorizeCommandMutation();
  const [resolveControl, resolving] = useResolveControlMutation();
  const [failure, setFailure] = useState<string | null>(null);
  const disabled = connectionId === null || authorizing.isLoading || resolving.isLoading;

  async function authorize(sessionId: string, commandId: string, approved: boolean) {
    if (connectionId === null) {
      return;
    }
    setFailure(null);
    try {
      await authorizeCommand({ connectionId, sessionId, commandId, approved }).unwrap();
    } catch {
      setFailure("操作没有完成");
    }
  }

  async function control(sessionId: string, requestId: string, approved: boolean) {
    if (connectionId === null) {
      return;
    }
    setFailure(null);
    try {
      await resolveControl({ connectionId, sessionId, requestId, approved }).unwrap();
    } catch {
      setFailure("操作没有完成");
    }
  }

  return (
    <Stack gap="md" maw={720} mx="auto" p="lg">
      <Text fw={700} size="lg">待处理</Text>
      {failure === null ? null : <Text c="red" size="sm">{failure}</Text>}
      {items.length === 0 ? (
        <Text c="dimmed">没有待处理事项</Text>
      ) : items.map((item) => (
        <InboxRow
          key={item.kind === "authorization" ? `authorization:${item.command_id}` : `control:${item.request_id}`}
          disabled={disabled}
          item={item}
          onAuthorize={authorize}
          onControl={control}
        />
      ))}
    </Stack>
  );
}
