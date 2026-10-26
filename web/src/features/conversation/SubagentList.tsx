import { Badge, Button, Group, Stack, Text } from "@mantine/core";
import { useGetSubagentsQuery } from "../../api/kondenApi";

const STATUS_LABEL = {
  creating: "创建中",
  running: "运行中",
  waiting: "等待中",
  closed: "已结束",
  failed: "失败",
};

export function SubagentList({ sessionId, onObserve }: {
  sessionId: string; onObserve: (commandId: string) => void;
}) {
  const query = useGetSubagentsQuery(sessionId);
  const subagents = query.currentData ?? [];
  return <section className="agent-details-section" aria-label="子智能体">
    <Group className="agent-details-label" justify="space-between" gap={6}>
      <Text size="xs" c="dimmed">子智能体</Text>
      {subagents.length === 0 ? null : <Text size="xs" c="dimmed">{subagents.length} 项</Text>}
    </Group>
    {query.isError ? <Text role="alert" c="red" size="xs">子智能体读取失败</Text>
      : query.isLoading ? <Text size="sm" c="dimmed">正在读取…</Text>
      : subagents.length === 0 ? <Text size="sm" c="dimmed">无子智能体</Text>
      : <Stack gap={0}>
        {subagents.slice().reverse().map((subagent) => <Stack className="subagent-summary-row" key={subagent.command_id} gap={6}>
          <Badge size="xs" variant="light"
            color={subagent.status === "failed" ? "red" : subagent.status === "running" ? "ember" : "gray"}>
            {STATUS_LABEL[subagent.status]}
          </Badge>
          <Text size="xs" className="subagent-summary-task" lineClamp={4} title={subagent.task}>{subagent.task}</Text>
          <Button size="compact-xs" variant="subtle" color="gray" className="subagent-summary-open"
            aria-label={`查看子智能体过程：${subagent.task}`}
            onClick={() => onObserve(subagent.command_id)}>查看过程</Button>
        </Stack>)}
      </Stack>}
  </section>;
}
