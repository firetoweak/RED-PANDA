import { Badge, Button, Collapse, Group, Paper, Stack, Text, UnstyledButton } from "@mantine/core";
import { IconChevronDown, IconChevronRight } from "@tabler/icons-react";
import { useState } from "react";

import type { EffectReview } from "../../api/contracts";
import { MarkdownMessage } from "./MarkdownMessage";

interface EffectReviewCanvasProps {
  review: EffectReview;
  source: "projection" | "sample";
  prose: string | null;
  disabled: boolean;
  onAuthorize?: (commandId: string, approved: boolean) => void;
  onRestore?: (stepId: string) => void;
}

export function EffectReviewCanvas({
  review,
  source,
  prose,
  disabled,
  onAuthorize,
  onRestore,
}: EffectReviewCanvasProps) {
  const [changesOpen, setChangesOpen] = useState(false);
  const [proseOpen, setProseOpen] = useState(false);
  const proseText = (prose ?? "").trim();

  return (
    <Paper className="effect-review" data-effect-review={source} p="sm" radius="md" withBorder>
      <Stack gap="sm">
        <Group justify="space-between" wrap="nowrap" gap="xs">
          <Text component="h3" fw={600} size="sm">效果核对</Text>
          {source === "sample" ? (
            <Badge color="ember" variant="light">实验样例</Badge>
          ) : null}
        </Group>
        <Text className="pre-wrap" size="sm">{review.conclusion}</Text>
        <Text c="dimmed" size="xs">
          {source === "sample"
            ? "实验样例，不是这条回复的内容。关掉实验开关后不会留在会话里。"
            : "这是这条回复的结构化视图。原来的正文还在下面。"}
        </Text>
        {review.metrics.length === 0 ? null : (
          <div className="effect-review-metrics">
            {review.metrics.map((metric, index) => (
              <Paper className="effect-review-metric" key={`${metric.label}-${index}`} p="xs" radius="sm" withBorder>
                <Text c="dimmed" size="xs">{metric.label}</Text>
                <Group gap={6} wrap="nowrap">
                  <Text size="sm">{formatMetric(metric.before, metric.unit)}</Text>
                  <Text c="dimmed" size="sm">→</Text>
                  <Text fw={600} size="sm">{formatMetric(metric.after, metric.unit)}</Text>
                </Group>
              </Paper>
            ))}
          </div>
        )}
        {review.changes.length === 0 ? null : (
          <div>
            <UnstyledButton
              aria-expanded={changesOpen}
              className="effect-review-changes-toggle"
              onClick={() => setChangesOpen((value) => !value)}
            >
              <Group gap={6} wrap="nowrap">
                {changesOpen ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />}
                <Text size="sm">改动 {review.changes.length} 处</Text>
              </Group>
            </UnstyledButton>
            <Collapse expanded={changesOpen}>
              {changesOpen ? (
                <Stack gap={6} pt={6}>
                  {review.changes.map((change, index) => (
                    <div key={`${change.path}-${index}`}>
                      <Text className="effect-review-path" size="sm">{change.path}</Text>
                      <Text c="dimmed" size="xs">{change.reason}</Text>
                    </div>
                  ))}
                </Stack>
              ) : null}
            </Collapse>
          </div>
        )}
        {review.actions.length === 0 ? null : (
          <Group align="flex-start" gap="sm">
            {review.actions.map((action, index) => {
              if (action.kind === "todo") {
                return (
                  <Stack gap={2} key={`${action.kind}-${index}`}>
                    <Button disabled size="compact-sm" variant="default">{action.label}</Button>
                    <Text c="dimmed" maw={280} size="xs">{action.note}</Text>
                  </Stack>
                );
              }
              if (action.kind === "authorize") {
                return (
                  <Button
                    disabled={disabled || onAuthorize === undefined}
                    key={`${action.kind}-${action.command_id}-${index}`}
                    onClick={() => onAuthorize?.(action.command_id, action.approved)}
                    size="compact-sm"
                    variant="light"
                  >
                    {action.label}
                  </Button>
                );
              }
              return (
                <Button
                  disabled={disabled || onRestore === undefined}
                  key={`${action.kind}-${action.step_id}-${index}`}
                  onClick={() => onRestore?.(action.step_id)}
                  size="compact-sm"
                  variant="default"
                >
                  {action.label}
                </Button>
              );
            })}
          </Group>
        )}
        {proseText === "" ? null : (
          <div>
            <UnstyledButton
              aria-expanded={proseOpen}
              className="effect-review-prose-toggle"
              onClick={() => setProseOpen((value) => !value)}
            >
              <Group gap={6} wrap="nowrap">
                {proseOpen ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />}
                <Text size="sm">模型原文</Text>
              </Group>
            </UnstyledButton>
            <Collapse expanded={proseOpen}>
              {proseOpen ? <MarkdownMessage content={proseText} streaming={false} /> : null}
            </Collapse>
          </div>
        )}
      </Stack>
    </Paper>
  );
}

function formatMetric(value: string, unit: string | null): string {
  return unit === null ? value : `${value} ${unit}`;
}
