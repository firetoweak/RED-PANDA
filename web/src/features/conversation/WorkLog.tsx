import { Box, Group, Loader, Stack, Text, UnstyledButton } from "@mantine/core";
import { IconChevronDown, IconChevronRight, IconNotebook } from "@tabler/icons-react";
import { useEffect, useState, type ReactNode } from "react";

import { formatMessageTime } from "./messageTime";
import { stepHeading } from "./stepHeading";
import type { TimelineTurn } from "./timelineTurns";

/**
 * 同事私信、群里轮到自己或例行任务开始的一轮，是 Agent 自己的工作记录，不是给人的回复：
 * 收成一行，点开才看全文与工具。只有要人操作（待授权）时自动展开。
 */
export function WorkLog({ turn, settled, children }: {
  turn: TimelineTurn;
  settled: boolean;
  children: ReactNode;
}) {
  const steps = [...turn.process, turn.active, turn.final].filter((step) => step !== null);
  const needsAction = steps.some((step) => step.tools.some((tool) => tool.status === "awaiting_authorization"));
  const [opened, setOpened] = useState(needsAction);

  useEffect(() => {
    if (needsAction) setOpened(true);
  }, [needsAction]);

  const startedAt = steps.find((step) => step.occurredAt !== null)?.occurredAt ?? null;
  const time = startedAt === null ? null : formatMessageTime(startedAt);
  const last = steps.at(-1);
  return (
    <Box className="work-log">
      <UnstyledButton aria-expanded={opened} className="work-log-toggle" onClick={() => setOpened((value) => !value)}>
        <Group gap={8} wrap="nowrap">
          {opened ? <IconChevronDown size={15} /> : <IconChevronRight size={15} />}
          {settled ? <IconNotebook size={15} /> : <Loader color="ember" size={12} />}
          <Text fw={600} size="sm" style={{ flexShrink: 0 }}>工作记录</Text>
          {time === null ? null : (
            <Text component="time" dateTime={startedAt ?? undefined} c="dimmed" fz={11}
              title={time.full} style={{ flexShrink: 0 }}>
              {time.short}
            </Text>
          )}
          {last === undefined ? null : <Text c="dimmed" size="sm" truncate>{stepHeading(last)}</Text>}
        </Group>
      </UnstyledButton>
      {opened ? <Stack className="work-log-body" gap="lg">{children}</Stack> : null}
    </Box>
  );
}
