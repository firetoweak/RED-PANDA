import { Collapse, Group, Paper, Stack, Text, UnstyledButton } from "@mantine/core";
import { IconChevronDown, IconChevronRight } from "@tabler/icons-react";
import { useState } from "react";

import type { WorkPlan } from "../../api/contracts";
import { isPlanCompleted } from "./workPlanPlacement";

const STATUS_LABEL = {
  pending: "待做",
  in_progress: "进行中",
  completed: "已完成",
};

export function WorkPlanPanel({ plan }: { plan: WorkPlan | null }) {
  const [opened, setOpened] = useState(false);
  if (plan === null) {
    return null;
  }
  const completed = plan.steps.filter((step) => step.status === "completed").length;
  const title = isPlanCompleted(plan) ? "已完成计划" : "当前计划";
  const ongoing = plan.steps.filter((step) => step.status === "in_progress");

  return (
    <Paper className="work-plan" radius="md" withBorder>
      <UnstyledButton
        className="work-plan-toggle"
        aria-label={title}
        aria-expanded={opened}
        onClick={() => setOpened((value) => !value)}
      >
        <Group justify="space-between" wrap="nowrap" gap="xs">
          <Stack gap={2} style={{ minWidth: 0 }}>
            <Text size="sm" fw={600} truncate>{title} · {plan.objective}</Text>
            {ongoing.length === 0 ? null : (
              <Text size="xs" c="dimmed" truncate>
                进行中：{ongoing.map((step) => step.text).join("、")}
              </Text>
            )}
          </Stack>
          <Group gap={8} wrap="nowrap" style={{ flexShrink: 0 }}>
            <Text size="xs" c="dimmed">已完成 {completed}/{plan.steps.length} 项</Text>
            {opened ? <IconChevronDown size={15} /> : <IconChevronRight size={15} />}
          </Group>
        </Group>
      </UnstyledButton>
      {plan.note === null ? null : (
        <Text size="xs" c="dimmed" className="pre-wrap" px="sm" pb="xs">{plan.note}</Text>
      )}
      <Collapse expanded={opened}>
        {opened ? (
          <Stack gap={6} px="sm" pb="sm">
            {plan.steps.map((step, index) => (
              <Group key={index} gap="sm" wrap="nowrap" align="flex-start">
                <Text size="xs" c={step.status === "in_progress" ? "ember" : "dimmed"} w={42} style={{ flexShrink: 0 }}>
                  {STATUS_LABEL[step.status]}
                </Text>
                <Text size="sm" className="pre-wrap">{step.text}</Text>
              </Group>
            ))}
          </Stack>
        ) : null}
      </Collapse>
    </Paper>
  );
}
