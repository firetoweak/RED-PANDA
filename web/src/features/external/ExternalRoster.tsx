import { Button, Group, Stack, Text, UnstyledButton } from "@mantine/core";
import { IconPlugConnected } from "@tabler/icons-react";
import { useMatch } from "react-router-dom";

import { useGetExternalSessionsQuery } from "../../api/kondenApi";
import { formatMessageTime } from "../conversation/messageTime";

export function ExternalRoster({ query, onOpen }: { query: string; onOpen: (path: string) => void }) {
  const identity = useMatch("/external/:identity")?.params.identity;
  const { data: sessions = [], isLoading, error, refetch } = useGetExternalSessionsQuery();
  const visible = sessions.filter((session) => session.name.toLocaleLowerCase().includes(query));
  return <section className="roster-section external-roster" aria-label="外部会话">
    <Group className="roster-section-header" justify="space-between">
      <Text fz={11}>外部会话</Text><span className="roster-section-count">{sessions.length}</span>
    </Group>
    {error !== undefined ? <Text c="dimmed" size="xs" px={10}>无法读取外部会话。<Button size="compact-xs" variant="subtle" onClick={() => void refetch()}>重试</Button></Text>
      : isLoading ? <Text c="dimmed" size="xs" px={10}>正在读取外部会话…</Text>
      : visible.length === 0 ? <Text c="dimmed" size="xs" px={10} py={8}>{query === "" ? "尚无外部会话，可在设置中配置外部接入。" : "没有匹配的外部会话。"}</Text> : null}
    <Stack gap={3}>{visible.map((session) => <div key={session.identity}
      className={identity === session.identity ? "session-row is-active" : "session-row"}>
      <UnstyledButton className="session-row-main" aria-label={session.name}
        aria-current={identity === session.identity ? "page" : undefined}
        onClick={() => onOpen(`/external/${encodeURIComponent(session.identity)}`)}>
        <Group gap={10} wrap="nowrap">
          <span className="agent-avatar agent-avatar-small" aria-hidden="true"><IconPlugConnected size={18} /></span>
          <Stack className="sidebar-agent-identity" gap={2}>
            <Text size="sm" truncate>{session.name}</Text>
            <Text c="dimmed" size="xs" truncate title={formatMessageTime(session.last_activity_at).full}>
              {session.active ? `最近活动 ${formatMessageTime(session.last_activity_at).short}` : "接入已撤销"}
            </Text>
          </Stack>
        </Group>
      </UnstyledButton>
    </div>)}</Stack>
  </section>;
}
