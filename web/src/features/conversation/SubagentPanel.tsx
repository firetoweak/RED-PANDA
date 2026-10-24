import { Alert, Badge, Button, Center, Group, Loader, Paper, ScrollArea, Stack, Text } from "@mantine/core";
import { IconArrowDown, IconChevronDown, IconChevronUp } from "@tabler/icons-react";
import { useState } from "react";

import { useObserveSubagentQuery } from "../../api/kondenApi";
import { useAppSelector } from "../../app/hooks";
import { StepDisclosure } from "./ExecutionProcess";
import { MarkdownMessage } from "./MarkdownMessage";
import { PrivateMessageNotice } from "./PrivateMessageNotice";
import { useFollowOutput } from "./useFollowOutput";
import { visibleTimeline } from "./visibleTimeline";

export function SubagentPanel({ parentSessionId, commandId }: {
  parentSessionId: string; commandId: string;
}) {
  const query = useObserveSubagentQuery({ parentSessionId, commandId });
  const [taskOpened, setTaskOpened] = useState(false);
  const observation = query.currentData;
  const runtime = useAppSelector((state) => observation === undefined ? undefined : state.runtime.sessions[observation.session_id]);
  const connected = useAppSelector((state) => state.runtime.connectionId !== null);
  const activity = runtime?.activity ?? observation?.activity;
  const result = observation?.result ?? null;
  const live = connected && (activity === "running" || runtime?.activePreview != null || runtime?.activeThinking != null);
  const follow = useFollowOutput(observation?.session_id, live, observation?.conversation != null);
  const conversation = observation?.conversation;
  const items = conversation == null ? [] : visibleTimeline(
    conversation, runtime?.committed ?? {}, runtime?.activePreview ?? null, runtime?.tools ?? {},
    runtime?.committedThinking ?? {}, runtime?.activeThinking ?? null, runtime?.activity ?? null,
  );
  const label = result !== null
    ? result.failure !== null ? "执行失败" : "协作已结束"
    : !connected ? "连接中断" : conversation === null ? "正在创建" : activity === "running" ? "运行中" : "等待中";

  return <div className="subagent-page">
    <Stack className="subagent-panel-header" gap={8}>
      <Group justify="space-between" wrap="nowrap">
        <Badge variant="light">{label}</Badge>
        {observation === undefined ? null : <Button
          aria-expanded={taskOpened}
          color="gray"
          onClick={() => setTaskOpened((value) => !value)}
          rightSection={taskOpened ? <IconChevronUp size={13} /> : <IconChevronDown size={13} />}
          size="compact-xs"
          variant="subtle"
        >{taskOpened ? "收起任务" : "展开任务"}</Button>}
      </Group>
      {observation === undefined ? null : taskOpened
        ? <ScrollArea.Autosize className="subagent-task-scroll" mah="40vh" type="hover">
          <Text className="pre-wrap" fz={12} c="dimmed">{observation.task}</Text>
        </ScrollArea.Autosize>
        : <Text className="pre-wrap" fz={12} c="dimmed" lineClamp={3}>{observation.task}</Text>}
    </Stack>
    {query.isError ? <Alert color="red" m="md">无法读取子任务过程</Alert>
      : observation === undefined ? <Center><Loader size="sm" /></Center>
      : <ScrollArea className="subagent-panel-scroll" viewportRef={follow.viewportRef} type="hover">
        <Stack className="subagent-panel-content" gap="md" ref={follow.contentRef}>
          {items.length === 0 ? <Text c="dimmed" size="sm">{conversation === null ? "正在创建子任务…" : "尚无执行记录"}</Text> : null}
          {items.length === 0 ? null : <Paper className="execution-process subagent-process" withBorder radius="md">
            <Stack className="execution-steps" gap={4}>
              {items.map((item, index) => item.kind === "user"
                ? <MarkdownMessage key={item.key} content={item.text} streaming={false} />
                : item.kind === "step" ? <StepDisclosure key={item.key} step={item} index={index} expandRunning />
                : <PrivateMessageNotice key={item.key} message={item} />)}
            </Stack>
          </Paper>}
          {runtime?.lastError == null ? null : <Alert color="red">{runtime.lastError}</Alert>}
          {result?.failure == null ? null : <Alert color="red" title="执行失败">{result.failure}</Alert>}
          {result?.reason == null ? null : <Text c="dimmed" size="sm">结束原因：{result.reason}</Text>}
        </Stack>
      </ScrollArea>}
    {follow.following ? null : <Button className="subagent-jump" size="compact-sm" variant="light"
      leftSection={<IconArrowDown size={14} />} onClick={follow.scrollToBottom}>回到最新</Button>}
  </div>;
}
