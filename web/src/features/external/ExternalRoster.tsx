import { Button, Group, Stack, Text, UnstyledButton } from "@mantine/core";
import { IconPlugConnected } from "@tabler/icons-react";
import { useState } from "react";
import { useMatch } from "react-router-dom";

import { useGetExternalSessionsQuery } from "../../api/kondenApi";
import { sessionStatus } from "./sessionStatus";

export function ExternalRoster({ query, onOpen }: { query: string; onOpen: (path: string) => void }) {
  const identity = useMatch("/external/:identity")?.params.identity;
  const { data: sessions = [], isLoading, error, refetch } = useGetExternalSessionsQuery();
  const [showRevoked, setShowRevoked] = useState(false);
  const revoked = sessions.filter((session) => !session.active);
  const visible = sessions.filter((session) => session.name.toLocaleLowerCase().includes(query));
  const entries = visible.filter((session) => session.active || showRevoked || query !== "" || session.identity === identity);
  return <section className="roster-section external-roster" aria-label="外部会话">
    <Group className="roster-section-header" justify="space-between">
      <Text fz={11}>外部会话</Text><span className="roster-section-count">{sessions.length - revoked.length}</span>
    </Group>
    {error !== undefined ? <Text c="dimmed" size="xs" px={10}>无法读取外部会话。<Button size="compact-xs" variant="subtle" onClick={() => void refetch()}>重试</Button></Text>
      : isLoading ? <Text c="dimmed" size="xs" px={10}>正在读取外部会话…</Text>
      : visible.length === 0 ? <Text c="dimmed" size="xs" px={10} py={8}>{query === "" ? "尚无外部会话，可在「设置 → Codex 接入」中配置。" : "没有匹配的外部会话。"}</Text> : null}
    <Stack gap={3}>{entries.map((session) => <div key={session.identity}
      className={identity === session.identity ? "session-row is-active" : "session-row"}>
      <UnstyledButton className="session-row-main" aria-label={session.name}
        aria-current={identity === session.identity ? "page" : undefined}
        onClick={() => onOpen(`/external/${encodeURIComponent(session.identity)}`)}>
        <Group gap={10} wrap="nowrap">
          <span className="agent-avatar agent-avatar-small" aria-hidden="true"><IconPlugConnected size={18} /></span>
          <Stack className="sidebar-agent-identity" gap={2}>
            <Text size="sm" truncate>{session.name}</Text>
            <Text c="dimmed" size="xs" truncate>
              {sessionStatus(session)}
            </Text>
          </Stack>
        </Group>
      </UnstyledButton>
    </div>)}</Stack>
    {revoked.length === 0 || query !== "" ? null : <Button variant="subtle" color="gray" size="compact-xs"
      aria-expanded={showRevoked} onClick={() => setShowRevoked(!showRevoked)}>
      {showRevoked ? "收起已撤销会话" : `已撤销会话（${revoked.length}）`}
    </Button>}
  </section>;
}
