import { ActionIcon, Alert, Button, Group, Menu, Modal, Stack, Text, UnstyledButton } from "@mantine/core";
import { IconDots, IconPlugConnected, IconTrash } from "@tabler/icons-react";
import { useState } from "react";
import { useMatch } from "react-router-dom";

import { useDeleteExternalSessionMutation, useGetExternalSessionsQuery } from "../../api/kondenApi";
import type { ExternalSessionSummary } from "../../api/contracts";
import { useAppSelector } from "../../app/hooks";
import { sessionStatus } from "./sessionStatus";

export function ExternalRoster({ query, onOpen }: { query: string; onOpen: (path: string) => void }) {
  const identity = useMatch("/external/:identity")?.params.identity;
  const { data: sessions = [], isLoading, error, refetch } = useGetExternalSessionsQuery();
  const [showRevoked, setShowRevoked] = useState(false);
  const [menuIdentity, setMenuIdentity] = useState<string | null>(null);
  const [removing, setRemoving] = useState<ExternalSessionSummary | null>(null);
  const revoked = sessions.filter((session) => !session.active);
  const visible = sessions.filter((session) => session.name.toLocaleLowerCase().includes(query));
  const entries = visible.filter((session) => session.active || showRevoked || query !== "" || session.identity === identity);
  return <section className="roster-section external-roster" aria-label="外部会话">
    {removing === null ? null : <RemoveExternalSessionModal target={removing} onClose={() => setRemoving(null)} onRemoved={() => {
      if (identity === removing.identity) onOpen("/");
      setRemoving(null);
    }} />}
    <Group className="roster-section-header" justify="space-between">
      <Text fz={11}>外部会话</Text><span className="roster-section-count">{sessions.length - revoked.length}</span>
    </Group>
    {error !== undefined ? <Text c="dimmed" size="xs" px={10}>无法读取外部会话。<Button size="compact-xs" variant="subtle" onClick={() => void refetch()}>重试</Button></Text>
      : isLoading ? <Text c="dimmed" size="xs" px={10}>正在读取外部会话…</Text>
      : visible.length === 0 ? <Text c="dimmed" size="xs" px={10} py={8}>{query === "" ? "尚无外部会话，可在「设置 → Codex 接入」中配置。" : "没有匹配的外部会话。"}</Text> : null}
    <Stack gap={3}>{entries.map((session) => <div key={session.identity}
      className={identity === session.identity ? "session-row is-active" : "session-row"}
      onContextMenu={(event) => { event.preventDefault(); setMenuIdentity(session.identity); }}>
      <UnstyledButton className="session-row-main" aria-label={session.name} title={session.name}
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
      <Menu opened={menuIdentity === session.identity} onChange={(opened) => setMenuIdentity(opened ? session.identity : null)} position="bottom-end" withinPortal>
        <Menu.Target><ActionIcon className="roster-entry-menu" aria-label={`${session.name} 操作`} color="gray" variant="subtle" size="xs"><IconDots size={13} /></ActionIcon></Menu.Target>
        <Menu.Dropdown><Menu.Item color="red" leftSection={<IconTrash size={14} />} onClick={() => setRemoving(session)}>删除外部会话</Menu.Item></Menu.Dropdown>
      </Menu>
    </div>)}</Stack>
    {revoked.length === 0 || query !== "" ? null : <Button variant="subtle" color="gray" size="compact-xs"
      aria-expanded={showRevoked} onClick={() => setShowRevoked(!showRevoked)}>
      {showRevoked ? "收起已撤销会话" : `已撤销会话（${revoked.length}）`}
    </Button>}
  </section>;
}

function RemoveExternalSessionModal({ target, onClose, onRemoved }: {
  target: Pick<ExternalSessionSummary, "identity" | "name">; onClose: () => void; onRemoved: () => void;
}) {
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const [remove, { isLoading }] = useDeleteExternalSessionMutation();
  const [error, setError] = useState<string | null>(null);
  async function confirm() {
    if (connectionId === null || isLoading) return;
    setError(null);
    try {
      await remove({ connectionId, identity: target.identity }).unwrap();
      onRemoved();
    } catch (cause) {
      setError(typeof cause === "string" ? cause : "删除失败，请重试。");
    }
  }
  return <Modal opened title={`删除外部会话「${target.name}」？`} onClose={onClose}
    closeOnClickOutside={!isLoading} closeOnEscape={!isLoading} withCloseButton={!isLoading}>
    <Stack gap="md">
      <Text size="sm">删除 konden 中的接入记录，停止收发和自动接续。Bot 对话中的已有往来保留。</Text>
      <Text size="sm" c="dimmed">原生聊天以后主动重新接入时，会话会重新出现在侧栏。</Text>
      {error === null ? null : <Alert color="red" role="alert">{error}</Alert>}
      <Group justify="flex-end">
        <Button data-autofocus variant="default" disabled={isLoading} onClick={onClose}>取消</Button>
        <Button color="red" loading={isLoading} disabled={connectionId === null} onClick={() => void confirm()}>删除外部会话</Button>
      </Group>
    </Stack>
  </Modal>;
}
