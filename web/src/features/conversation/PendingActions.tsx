import { Button, Collapse, Group, Paper, Stack, Text, UnstyledButton } from "@mantine/core";
import { IconCheck, IconX } from "@tabler/icons-react";
import { useState } from "react";

import type { ConversationView, PendingAuthorization } from "../../api/contracts";

type ControlApproval = NonNullable<ConversationView["session"]["control_approval"]>;

function argumentSummary(args: Record<string, unknown>): string | null {
  const path = args.path;
  if (typeof path === "string") {
    return path;
  }
  const first = Object.values(args).find((value) => typeof value === "string");
  return typeof first === "string" ? first.split("\n")[0] : null;
}

function AuthorizationRow({ item, disabled, onAuthorize }: {
  item: PendingAuthorization;
  disabled: boolean;
  onAuthorize: (commandId: string, approved: boolean) => void;
}) {
  const [opened, setOpened] = useState(false);
  const summary = argumentSummary(item.arguments);
  return (
    <Stack gap={4}>
      <Group justify="space-between" wrap="nowrap" gap="xs">
        <UnstyledButton aria-expanded={opened} aria-label={`查看 ${item.name} 的参数`}
          onClick={() => setOpened((value) => !value)} style={{ minWidth: 0 }}>
          <Text ff="monospace" fz={13} truncate>
            {item.name}{summary === null ? "" : ` · ${summary}`}
          </Text>
        </UnstyledButton>
        <Group gap="xs" wrap="nowrap">
          <Button aria-label={`允许 ${item.name}`} color="ember" disabled={disabled}
            leftSection={<IconCheck size={14} />} onClick={() => onAuthorize(item.command_id, true)}
            size="compact-sm" variant="light">
            允许
          </Button>
          <Button aria-label={`拒绝 ${item.name}`} color="gray" disabled={disabled}
            leftSection={<IconX size={14} />} onClick={() => onAuthorize(item.command_id, false)}
            size="compact-sm" variant="light">
            拒绝
          </Button>
        </Group>
      </Group>
      <Collapse expanded={opened}>
        <Text c="dimmed" className="pre-wrap" ff="monospace" fz={11}>
          {JSON.stringify(item.arguments, null, 2)}
        </Text>
      </Collapse>
    </Stack>
  );
}

/** Pending decisions of this session, read from the server view so they disappear with the server state. */
export function PendingActions({ label = "待处理", authorizations, control, authorizationDisabled, controlDisabled, onAuthorize, onControl }: {
  label?: string;
  authorizations: PendingAuthorization[];
  control: ControlApproval | null;
  authorizationDisabled: boolean;
  controlDisabled: boolean;
  onAuthorize: (commandId: string, approved: boolean) => void;
  onControl: (approved: boolean) => void;
}) {
  const count = authorizations.length + (control === null ? 0 : 1);
  if (count === 0) {
    return null;
  }
  return (
    <Paper aria-label="待处理" className="pending-actions" component="section" mb={8} p="sm" radius="md" withBorder>
      <Stack gap="sm">
        <Text fw={600} size="sm">{label} · {count} 项</Text>
        {authorizations.map((item) => (
          <AuthorizationRow key={item.command_id} disabled={authorizationDisabled} item={item} onAuthorize={onAuthorize} />
        ))}
        {control === null ? null : (
          <Stack gap={4}>
            <Text className="pre-wrap" fz={13}>{control.summary}</Text>
            <Text c="dimmed" className="pre-wrap" fz={12}>风险：{control.risk}</Text>
            <Group justify="flex-end" gap="xs">
              <Button color="gray" disabled={controlDisabled} leftSection={<IconX size={14} />}
                onClick={() => onControl(false)} size="compact-sm" variant="light">
                取消
              </Button>
              <Button color="ember" disabled={controlDisabled} leftSection={<IconCheck size={14} />}
                onClick={() => onControl(true)} size="compact-sm" variant="light">
                确认
              </Button>
            </Group>
          </Stack>
        )}
      </Stack>
    </Paper>
  );
}
