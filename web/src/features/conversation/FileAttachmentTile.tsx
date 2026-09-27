import { ActionIcon, Anchor, Button, Group, Loader, Paper, Stack, Text } from "@mantine/core";
import { IconFile, IconX } from "@tabler/icons-react";

export function FileAttachmentTile({ name, size, href, state = "done", error, onRemove, onRetry }: {
  name: string;
  size: number;
  href?: string;
  state?: "uploading" | "done" | "error";
  error?: string | null;
  onRemove?: () => void;
  onRetry?: () => void;
}) {
  return (
    <Paper className="file-attachment" p="xs" radius="md" withBorder data-state={state}>
      <Group gap={8} wrap="nowrap">
        {state === "uploading" ? <Loader size={18} /> : <IconFile size={22} />}
        <Stack gap={2} style={{ minWidth: 0, flex: 1 }}>
          {href === undefined ? <Text size="sm" truncate title={name}>{name}</Text>
            : <Anchor href={href} download size="sm" truncate title={name}>{name}</Anchor>}
          <Text size="xs" c={state === "error" ? "red" : "dimmed"}>
            {state === "error" ? error ?? "上传失败" : `${formatFileSize(size)}${state === "uploading" ? " · 上传中" : ""}`}
          </Text>
        </Stack>
        {state === "error" && onRetry !== undefined ? <Button size="compact-xs" variant="subtle" onClick={onRetry}>重试</Button> : null}
        {onRemove === undefined ? null : <ActionIcon aria-label={`Remove ${name}`} variant="subtle" color="gray" onClick={onRemove}><IconX size={14} /></ActionIcon>}
      </Group>
    </Paper>
  );
}

function formatFileSize(size: number) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}
