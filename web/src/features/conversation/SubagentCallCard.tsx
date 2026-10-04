import {
  Alert,
  Badge,
  Button,
  Group,
  Loader,
  Paper,
  Text,
  ThemeIcon,
  UnstyledButton,
} from "@mantine/core";
import {
  IconAlertCircle,
  IconAlertTriangle,
  IconCheck,
  IconCode,
} from "@tabler/icons-react";
import { useState } from "react";

import type { ToolStatus } from "../../api/contracts";
import {
  subagentChildSessionId,
  subagentTask,
} from "./subagent";
import type { VisibleTool } from "./visibleTimeline";

const STATUS_LABEL: Record<ToolStatus, string> = {
  queued: "排队中",
  running: "运行中",
  succeeded: "完成",
  failed: "失败",
  unknown: "中断",
  awaiting_authorization: "待授权",
  rejected: "已拒绝",
};

const STATUS_COLOR: Record<ToolStatus, string> = {
  queued: "gray",
  running: "ember",
  succeeded: "gray",
  failed: "red",
  unknown: "yellow",
  awaiting_authorization: "orange",
  rejected: "gray",
};

export function SubagentCallCard({ tool, onObserve }: {
  tool: VisibleTool;
  onObserve?: (commandId: string) => void;
}) {
  const [detailsOpen, setDetailsOpen] = useState(false);
  const task = subagentTask(tool);
  const childId = subagentChildSessionId(tool);
  const hasDetails = task !== null || childId !== null;

  return (
    <Paper className="tool-card subagent-call" px="sm" py={6} radius="sm" withBorder>
      <UnstyledButton
        className="tool-toggle"
        onClick={() => {
          if (hasDetails) {
            setDetailsOpen((value) => !value);
          }
        }}
      >
        <Group justify="space-between" wrap="nowrap" gap="xs">
          <Group gap={8} wrap="nowrap" style={{ minWidth: 0 }}>
            <ThemeIcon
              color={STATUS_COLOR[tool.status]}
              radius="sm"
              size={22}
              variant="light"
            >
              {tool.status === "running" ? (
                <Loader color="ember" size={11} />
              ) : tool.status === "failed" ? (
                <IconAlertCircle size={13} />
              ) : tool.status === "unknown" ? (
                <IconAlertTriangle size={13} />
              ) : (
                <IconCode size={13} />
              )}
            </ThemeIcon>
            <Text ff="monospace" fw={600} size="sm" truncate>
              {tool.name}
            </Text>
          </Group>
          <Badge
            color={STATUS_COLOR[tool.status]}
            leftSection={
              tool.status === "succeeded" ? <IconCheck size={10} /> : undefined
            }
            size="xs"
            variant="light"
          >
            {tool.name === "delegate" && tool.status === "succeeded" ? "已派出" : STATUS_LABEL[tool.status]}
          </Badge>
        </Group>
      </UnstyledButton>
      {tool.name === "delegate" && onObserve !== undefined ? (
        <Group justify="space-between" gap="xs" mt={6} wrap="nowrap">
          <Text c="dimmed" fz={12} truncate>{task}</Text>
          <Button size="compact-xs" variant="subtle"
            disabled={tool.status === "failed" || tool.status === "rejected"}
            onClick={() => onObserve(tool.commandId)}>查看过程</Button>
        </Group>
      ) : null}
      {detailsOpen && task !== null ? (
        <Text c="dimmed" className="pre-wrap" fz={12} mt={6}>
          {task}
        </Text>
      ) : null}
      {detailsOpen && childId !== null ? (
        <Text c="dimmed" ff="monospace" fz={11} mt={6}>
          {childId}
        </Text>
      ) : null}
      {tool.error === null ? null : (
        <Alert color={STATUS_COLOR[tool.status]} mt="xs" py="xs" variant="light">
          <Text ff="monospace" fz={12} className="pre-wrap">
            {tool.error}
          </Text>
        </Alert>
      )}
    </Paper>
  );
}
