import { Collapse, Group, Paper, Text, Tooltip, UnstyledButton } from "@mantine/core";
import { IconChevronDown, IconChevronRight, IconCircle, IconCircleCheck, IconCircleDot, IconListCheck, IconPlayerPause } from "@tabler/icons-react";
import { useState } from "react";

import type { WorkPlan } from "../../api/contracts";
import { isPlanCompleted } from "./workPlanPlacement";

const STATUS_LABEL = {
  pending: "待做",
  in_progress: "进行中",
  completed: "已完成",
};

const STATUS_ICON = {
  pending: IconCircle,
  in_progress: IconCircleDot,
  completed: IconCircleCheck,
};

export function WorkPlanPanel({ plan, stopped = false }: { plan: WorkPlan | null; stopped?: boolean }) {
  const [opened, setOpened] = useState(false);
  if (plan === null) {
    return null;
  }
  const completed = plan.steps.filter((step) => step.status === "completed").length;
  const title = isPlanCompleted(plan) ? "已完成计划" : "当前计划";
  const countLabel = `已完成 ${completed}/${plan.steps.length} 项`;
  const progressLabel = `模型记录的计划进度${stopped ? " · 执行已停止" : ""}`;
  const PlanIcon = isPlanCompleted(plan) ? IconCircleCheck : IconListCheck;

  return (
    <Paper className="work-plan" radius="md" withBorder>
      <UnstyledButton
        className="work-plan-toggle"
        aria-label={title}
        aria-description={`${plan.objective}。${progressLabel}。${countLabel}`}
        aria-expanded={opened}
        onClick={() => setOpened((value) => !value)}
      >
        <Group justify="space-between" wrap="nowrap" gap="xs">
          <Group className="work-plan-heading" gap={8} wrap="nowrap">
            <Tooltip label={title}><span className="work-plan-symbol"><PlanIcon aria-hidden="true" size={17} /></span></Tooltip>
            <Text size="sm" fw={600} truncate title={plan.objective}>{plan.objective}</Text>
          </Group>
          <Group gap={8} wrap="nowrap" style={{ flexShrink: 0 }}>
            <span className="work-plan-count" aria-label={countLabel} title={countLabel}>{completed}/{plan.steps.length}</span>
            {opened ? <IconChevronDown size={15} /> : <IconChevronRight size={15} />}
          </Group>
        </Group>
        <div className="work-plan-summary">
          <span className="work-plan-origin" title="模型记录的计划进度">模型标记</span>
          <span className="work-plan-track" role="group" aria-label="模型记录的计划进度">
            {plan.steps.map((step, index) => {
              const StepIcon = STATUS_ICON[step.status];
              return <Tooltip key={index} label={`${STATUS_LABEL[step.status]} · ${step.text}`}>
                <span className="work-plan-track-item" data-status={step.status} role="img" aria-label={`${STATUS_LABEL[step.status]}：${step.text}`}>
                  <StepIcon aria-hidden="true" size={16} />
                </span>
              </Tooltip>;
            })}
          </span>
          {stopped ? <span className="work-plan-stopped"><IconPlayerPause aria-hidden="true" size={12} />执行已停止</span> : null}
        </div>
      </UnstyledButton>
      {plan.note === null ? null : (
        <Text size="xs" c="dimmed" className="pre-wrap" px="sm" pb="xs">{plan.note}</Text>
      )}
      <Collapse expanded={opened}>
        {opened ? (
          <ol className="work-plan-steps" aria-label="计划事项">
            {plan.steps.map((step, index) => {
              const StepIcon = STATUS_ICON[step.status];
              return <li key={index} data-status={step.status}>
                <span className="work-plan-marker" data-status={step.status} role="img" aria-label={STATUS_LABEL[step.status]}><StepIcon aria-hidden="true" size={16} /></span>
                <Text size="sm" className="pre-wrap">{step.text}</Text>
              </li>;
            })}
          </ol>
        ) : null}
      </Collapse>
    </Paper>
  );
}
