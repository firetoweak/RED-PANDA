import { Alert, Button, Group, ScrollArea, Stack, Text } from "@mantine/core";
import { useClipboard } from "@mantine/hooks";
import { IconAlertCircle, IconCheck, IconCopy } from "@tabler/icons-react";
import type { ReactNode } from "react";

export function ErrorNotice({ title, message, children, onOpen }: {
  title: string;
  message: string;
  children?: ReactNode;
  onOpen: (message: string) => void;
}) {
  return (
    <Alert className="composer-error" color="red" icon={<IconAlertCircle size={16} />} py="xs" role="alert"
      styles={{ body: { minWidth: 0 } }}>
      <Group gap="sm" justify="space-between" wrap="nowrap">
        <Text fz={11} truncate style={{ flex: 1, minWidth: 0 }}>
          {title}。{message.split(/\r?\n/, 1)[0]}
        </Text>
        <Group gap="xs" wrap="nowrap" style={{ flexShrink: 0 }}>
          <Button size="compact-xs" variant="white" onClick={() => onOpen(message)}>
            查看详情
          </Button>
          {children}
        </Group>
      </Group>
    </Alert>
  );
}

export function ErrorDetails({ message }: { message: string }) {
  const clipboard = useClipboard();
  return (
    <div className="error-details-page">
      <Stack className="content-page-toolbar" gap="xs">
        <Group>
          <Button size="xs" variant="light" onClick={() => clipboard.copy(message)}
            leftSection={clipboard.copied ? <IconCheck size={14} /> : <IconCopy size={14} />}>
            {clipboard.copied ? "已复制" : "复制完整错误"}
          </Button>
        </Group>
        {clipboard.error === null ? null : <Text size="xs" c="red">复制失败：{clipboard.error.message}</Text>}
      </Stack>
      <ScrollArea className="content-page-scroll" type="hover">
        <Text component="pre" className="pre-wrap" ff="monospace" fz={12} m={0}>
          {message}
        </Text>
      </ScrollArea>
    </div>
  );
}
