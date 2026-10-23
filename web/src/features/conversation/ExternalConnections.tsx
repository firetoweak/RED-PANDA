import { Alert, Button, Divider, Group, Modal, Stack, Text, Textarea, TextInput } from "@mantine/core";
import { useClipboard } from "@mantine/hooks";
import { useState } from "react";

import type { CreatedExternalConnection } from "../../api/contracts";
import { useCreateExternalConnectionMutation, useGetExternalConnectionsQuery, useRevokeExternalConnectionMutation } from "../../api/redpandaApi";

type Props = { sessionId: string; botName: string; connectionId: string | null };

export function ExternalConnections(props: Props) {
  const [opened, setOpened] = useState(false);
  return <div className="agent-details-section">
    <Text className="agent-details-label" size="xs" c="dimmed">外部接入</Text>
    <Text size="xs" c="dimmed">让外部编码智能体通过 MCP 联系这位 Bot。</Text>
    <Button mt="xs" size="compact-xs" variant="light" onClick={() => setOpened(true)}>管理外部接入</Button>
    {opened ? <ExternalConnectionsDialog {...props} onClose={() => setOpened(false)} /> : null}
  </div>;
}

function ExternalConnectionsDialog({ sessionId, botName, connectionId, onClose }: Props & { onClose: () => void }) {
  const { currentData: connections = [], isLoading, error: loadError, refetch } = useGetExternalConnectionsQuery(sessionId);
  const [createConnection, creating] = useCreateExternalConnectionMutation();
  const [revokeConnection, revoking] = useRevokeExternalConnectionMutation();
  const [name, setName] = useState("");
  const [created, setCreated] = useState<CreatedExternalConnection | null>(null);
  const [confirmRevoke, setConfirmRevoke] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const clipboard = useClipboard();
  const busy = creating.isLoading || revoking.isLoading;
  const config = created === null ? "" : JSON.stringify({ mcpServers: {
    red_panda: { url: created.url, headers: { Authorization: `Bearer ${created.token}` } },
  } }, null, 2);

  async function create() {
    if (connectionId === null || name.trim() === "" || busy) return;
    setError(null);
    clipboard.reset();
    const request = createConnection({ connectionId, sessionId, name: name.trim() });
    try {
      setCreated(await request.unwrap());
      setName("");
    } catch (cause) {
      setError(typeof cause === "string" ? cause : "创建失败，请检查连接后重试。");
    } finally {
      request.reset();
    }
  }

  async function revoke(externalId: string) {
    if (connectionId === null || busy) return;
    setError(null);
    try {
      await revokeConnection({ connectionId, sessionId, externalId }).unwrap();
      setConfirmRevoke(null);
      if (created?.connection.connection_id === externalId) {
        setCreated(null);
        clipboard.reset();
      }
    } catch (cause) {
      setError(typeof cause === "string" ? cause : "撤销失败，请检查连接后重试。");
    }
  }

  return <Modal opened onClose={() => { if (!busy) onClose(); }} title={`${botName} 的外部接入`} size="lg"
    closeButtonProps={{ "aria-label": "关闭外部接入" }}
    closeOnClickOutside={!busy} closeOnEscape={!busy} withCloseButton={!busy}>
    <Stack gap="md">
      <Text size="sm" c="dimmed">每份接入只联系这位 Bot。配置可供多个聊天复用，每个聊天由工具领取独立身份，分别收发消息。</Text>
      {created === null ? <form onSubmit={(event) => { event.preventDefault(); void create(); }}>
        <Stack gap="sm">
          <TextInput label="接入名称" placeholder="例如：我的编码端" value={name}
            onChange={(event) => setName(event.currentTarget.value)} disabled={busy} maxLength={200} />
          <Group justify="flex-end">
            <Button type="submit" loading={creating.isLoading} disabled={connectionId === null || name.trim() === "" || busy}>创建接入</Button>
          </Group>
        </Stack>
      </form> : <Stack gap="sm">
        <Text fw={600} size="sm">{created.connection.name} 已创建</Text>
        <Text size="sm">请保存配置。凭据只在本次创建后显示，关闭窗口后无法再次查看；遗失时请撤销并重新创建。</Text>
        <Textarea label="MCP 配置（JSON）" value={config} readOnly autosize minRows={8} maxRows={12}
          classNames={{ input: "external-connection-config" }} />
        <Text size="xs" c="dimmed">支持此格式的客户端可使用这段配置；其他客户端填写其中的 URL 和 Authorization 请求头。凭据用于连接，请勿粘贴到聊天正文。</Text>
        <Group justify="space-between">
          <Button variant="subtle" onClick={() => { setCreated(null); clipboard.reset(); }}>已保存，继续管理</Button>
          <Button onClick={() => clipboard.copy(config)}>{clipboard.copied ? "已复制" : "复制配置"}</Button>
        </Group>
        {clipboard.error === null ? null : <Alert color="orange" role="alert">复制失败，请选中上方配置手动复制。</Alert>}
      </Stack>}
      {error === null ? null : <Alert color="red" role="alert">{error}</Alert>}
      {connectionId === null ? <Text size="sm" c="dimmed">正在连接，恢复后可创建或撤销接入。</Text> : null}
      <Divider />
      <Stack gap="sm">
        <Text fw={600} size="sm">已有接入</Text>
        {loadError !== undefined ? <Alert color="orange" role="alert">
          无法读取接入列表。<Button size="compact-xs" variant="subtle" onClick={() => void refetch()}>重试</Button>
        </Alert> : isLoading ? <Text size="sm" c="dimmed">正在读取…</Text>
          : connections.length === 0 ? <Text size="sm" c="dimmed">还没有外部接入。创建后即可复制连接配置。</Text> : null}
        {connections.map((item) => <div key={item.connection_id}>
          <Group justify="space-between" wrap="nowrap">
            <Text size="sm" className="external-connection-name">{item.name}</Text>
            <Button size="compact-xs" variant="subtle" color="red" disabled={connectionId === null || busy}
              aria-label={`撤销 ${item.name}`} onClick={() => setConfirmRevoke(item.connection_id)}>撤销</Button>
          </Group>
          {confirmRevoke !== item.connection_id ? null : <Stack gap="xs" mt="xs">
            <Text size="sm">撤销后，此接入的所有聊天立即失去收发能力，已有消息保留。</Text>
            <Group gap="xs" justify="flex-end">
              <Button size="compact-xs" variant="subtle" disabled={busy} onClick={() => setConfirmRevoke(null)}>取消</Button>
              <Button size="compact-xs" color="red" loading={revoking.isLoading} disabled={connectionId === null || busy}
                onClick={() => void revoke(item.connection_id)}>确认撤销</Button>
            </Group>
          </Stack>}
        </div>)}
      </Stack>
    </Stack>
  </Modal>;
}
