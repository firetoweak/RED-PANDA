import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Collapse,
  Group,
  Loader,
  Paper,
  Stack,
  Text,
  ThemeIcon,
  Tooltip,
  UnstyledButton,
} from "@mantine/core";
import {
  IconAlertCircle,
  IconAlertTriangle,
  IconArrowBackUp,
  IconCheck,
  IconChevronDown,
  IconChevronRight,
  IconCode,
  IconSparkles,
  IconX,
} from "@tabler/icons-react";
import { useEffect, useState } from "react";

import type { ToolStatus } from "../../api/contracts";
import { stepHeading } from "./stepHeading";
import { isSubagentTool } from "./subagent";
import { SubagentCallCard } from "./SubagentCallCard";
import type { VisibleStep, VisibleTool } from "./visibleTimeline";
import { MarkdownMessage } from "./MarkdownMessage";
import { ThinkingBlock } from "./ThinkingBlock";

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
  running: "sage",
  succeeded: "gray",
  failed: "red",
  unknown: "yellow",
  awaiting_authorization: "orange",
  rejected: "gray",
};

interface ExecutionProcessProps {
  complete: boolean;
  steps: VisibleStep[];
  authorizationDisabled: boolean;
  onAuthorize: (commandId: string, approved: boolean) => void;
  onRestart: (stepId: string) => void;
  restartDisabled: boolean;
}

export function ExecutionProcess({
  complete,
  steps,
  authorizationDisabled,
  onAuthorize,
  onRestart,
  restartDisabled,
}: ExecutionProcessProps) {
  const [opened, setOpened] = useState(!complete);
  const running = steps.some(stepStatusIsRunning);
  const toolCount = steps.reduce((count, step) => count + step.tools.length, 0);

  useEffect(() => {
    if (complete) {
      setOpened(false);
    }
  }, [complete]);

  return (
    <Paper className="execution-process" radius="md" withBorder>
      <UnstyledButton
        aria-expanded={opened}
        className="execution-toggle"
        onClick={() => setOpened((value) => !value)}
      >
        <Group justify="space-between" wrap="nowrap" gap="xs">
          <Group gap={8} wrap="nowrap">
            <ThemeIcon color="gray" radius="sm" size={22} variant="light">
              {running ? <Loader color="sage" size={12} /> : <IconSparkles size={13} />}
            </ThemeIcon>
            <Text fw={600} size="sm">
              执行过程
            </Text>
            <Text c="dimmed" fz={11}>
              {steps.length} 步 · {toolCount} 次工具
            </Text>
          </Group>
          {opened ? <IconChevronDown size={15} /> : <IconChevronRight size={15} />}
        </Group>
      </UnstyledButton>
      <Collapse expanded={opened}>
        <Stack className="execution-steps" gap={4}>
          {steps.map((step, index) => (
            <StepDisclosure
              authorizationDisabled={authorizationDisabled}
              index={index}
              key={step.key}
              onAuthorize={onAuthorize}
              onRestart={onRestart}
              restartDisabled={restartDisabled}
              step={step}
            />
          ))}
        </Stack>
      </Collapse>
    </Paper>
  );
}

function StepDisclosure({
  index,
  step,
  authorizationDisabled,
  onAuthorize,
  onRestart,
  restartDisabled,
}: {
  index: number;
  step: VisibleStep;
  authorizationDisabled: boolean;
  onAuthorize: (commandId: string, approved: boolean) => void;
  onRestart: (stepId: string) => void;
  restartDisabled: boolean;
}) {
  const status = stepStatus(step);
  const awaiting = step.tools.some(
    (tool) => tool.status === "awaiting_authorization",
  );
  const [opened, setOpened] = useState(awaiting);

  useEffect(() => {
    // 默认一律折叠：标题已经给出这一步的意图，够用户判断执行有没有走偏；
    // 想看执行细节（思考块、工具与参数）由用户自己展开。
    // 只有等待授权这类需要用户操作的状态才自动展开。
    setOpened(awaiting);
  }, [awaiting]);

  return (
    <div className="step-panel">
      <Group gap={4} wrap="nowrap">
        <UnstyledButton
          aria-expanded={opened}
          className="step-toggle"
          onClick={() => setOpened((value) => !value)}
          style={{ flex: 1, minWidth: 0 }}
        >
          <Group justify="space-between" wrap="nowrap" gap="xs">
            <Group gap={8} wrap="nowrap" maw="100%" style={{ minWidth: 0 }}>
              {opened ? <IconChevronDown size={13} /> : <IconChevronRight size={13} />}
              <Text c="dimmed" fz={11} w={16} ta="right">
                {index + 1}
              </Text>
              <Text className="step-heading" size="sm" truncate>
                {stepHeading(step)}
              </Text>
            </Group>
            {/* 「完成」不带信息，每一步跑完都是它；让位给重开控件。 */}
            {status === "succeeded" ? null : (
              <Badge color={STATUS_COLOR[status]} size="xs" variant="light">
                {STATUS_LABEL[status]}
              </Badge>
            )}
          </Group>
        </UnstyledButton>
        {step.rewindable && step.stepId !== null ? (
          <Tooltip label="从这一步之后重开：截断对话，文件一起退回">
            <ActionIcon
              aria-label="从这一步之后重开"
              disabled={restartDisabled}
              onClick={() => onRestart(step.stepId!)}
              radius="xl"
              size="sm"
              variant="subtle"
            >
              <IconArrowBackUp size={14} />
            </ActionIcon>
          </Tooltip>
        ) : null}
      </Group>
      <Collapse expanded={opened}>
        {opened ? (
          <Stack className="step-content" gap="xs">
            {step.text === null ? null : (
              <MarkdownMessage content={step.text} streaming={step.pending} />
            )}
            {step.thinking === null ? null : (
              <ThinkingBlock
                streaming={step.thinkingPending}
                text={step.thinking}
              />
            )}
            {step.tools.map((tool) =>
              isSubagentTool(tool.name) ? (
                <SubagentCallCard key={tool.commandId} tool={tool} />
              ) : (
                <ToolCard
                  authorizationDisabled={authorizationDisabled}
                  key={tool.commandId}
                  onAuthorize={onAuthorize}
                  tool={tool}
                />
              ),
            )}
          </Stack>
        ) : null}
      </Collapse>
    </div>
  );
}

function ToolCard({
  tool,
  authorizationDisabled,
  onAuthorize,
}: {
  tool: VisibleTool;
  authorizationDisabled: boolean;
  onAuthorize: (commandId: string, approved: boolean) => void;
}) {
  const awaiting = tool.status === "awaiting_authorization";
  const hasArguments = Object.keys(tool.arguments).length > 0;
  const [detailsOpen, setDetailsOpen] = useState(false);

  return (
    <Paper className="tool-card" px="sm" py={6} radius="sm" withBorder>
      <UnstyledButton
        className="tool-toggle"
        onClick={() => {
          if (hasArguments) {
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
                <Loader color="sage" size={11} />
              ) : tool.status === "failed" ? (
                <IconAlertCircle size={13} />
              ) : tool.status === "unknown" ? (
                <IconAlertTriangle size={13} />
              ) : (
                <IconCode size={13} />
              )}
            </ThemeIcon>
            <Text ff="monospace" fw={600} size="sm" truncate>
              {tool.name === "update_plan"
                ? tool.arguments.plan === null ? "结束当前计划" : "更新计划"
                : tool.name}
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
            {STATUS_LABEL[tool.status]}
          </Badge>
        </Group>
      </UnstyledButton>
      {detailsOpen && hasArguments ? (
        <Text c="dimmed" className="pre-wrap" ff="monospace" fz={11} mt={6}>
          {formatArguments(tool.arguments)}
        </Text>
      ) : null}
      {awaiting ? (
        <Group gap="xs" mt="xs">
          <Button
            color="sage"
            disabled={authorizationDisabled}
            leftSection={<IconCheck size={14} />}
            onClick={() => onAuthorize(tool.commandId, true)}
            size="compact-sm"
            variant="light"
          >
            允许
          </Button>
          <Button
            color="gray"
            disabled={authorizationDisabled}
            leftSection={<IconX size={14} />}
            onClick={() => onAuthorize(tool.commandId, false)}
            size="compact-sm"
            variant="light"
          >
            拒绝
          </Button>
        </Group>
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

function formatArguments(arguments_: Record<string, unknown>) {
  return JSON.stringify(arguments_, null, 2);
}

function stepStatus(step: VisibleStep): ToolStatus {
  if (step.tools.some((tool) => tool.status === "awaiting_authorization")) {
    return "awaiting_authorization";
  }
  if (step.pending || step.tools.some((tool) => tool.status === "running")) {
    return "running";
  }
  if (step.tools.some((tool) => tool.status === "queued")) {
    return "queued";
  }
  if (step.tools.some((tool) => tool.status === "unknown")) {
    return "unknown";
  }
  if (
    step.tools.some(
      (tool) => tool.status === "failed" || tool.status === "rejected",
    )
  ) {
    return "rejected";
  }
  return "succeeded";
}

function stepStatusIsRunning(step: VisibleStep): boolean {
  const status = stepStatus(step);
  return (
    status === "queued" ||
    status === "running" ||
    status === "awaiting_authorization"
  );
}
