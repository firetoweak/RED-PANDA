import { Alert, Button, Divider, Group, Stack, Text, Textarea } from "@mantine/core";
import { useClipboard } from "@mantine/hooks";
import { useState } from "react";

import type { CreatedExternalConnection } from "../../api/contracts";
import { useCreateExternalConnectionMutation, useGetExternalConnectionsQuery, useRevokeExternalConnectionMutation } from "../../api/kondenApi";
import { useAppSelector } from "../../app/hooks";

export function VerificationAccess() {
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const { currentData: connections = [], isLoading, error: loadError, refetch } = useGetExternalConnectionsQuery();
  const [createConnection, creating] = useCreateExternalConnectionMutation();
  const [revokeConnection, revoking] = useRevokeExternalConnectionMutation();
  const [created, setCreated] = useState<CreatedExternalConnection | null>(null);
  const [confirmRevoke, setConfirmRevoke] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const clipboard = useClipboard();
  const busy = creating.isLoading || revoking.isLoading;
  const config = created === null ? "" : JSON.stringify({ mcpServers: {
    konden: { url: created.url, headers: { Authorization: `Bearer ${created.token}` } },
  } }, null, 2);

  async function create() {
    if (connectionId === null || busy) return;
    setError(null);
    clipboard.reset();
    const request = createConnection({ connectionId });
    try {
      setCreated(await request.unwrap());
    } catch (cause) {
      setError(typeof cause === "string" ? cause : "生成失败，请检查连接后重试。");
    } finally {
      request.reset();
    }
  }

  async function revoke(externalId: string) {
    if (connectionId === null || busy) return;
    setError(null);
    try {
      await revokeConnection({ connectionId, externalId }).unwrap();
      setConfirmRevoke(null);
      if (created?.connection.connection_id === externalId) {
        setCreated(null);
        clipboard.reset();
      }
    } catch (cause) {
      setError(typeof cause === "string" ? cause : "撤销失败，请检查连接后重试。");
    }
  }

  return <Stack gap="md">
    <Text size="sm">把连接配置添加到你使用的编码工具。完成修改后，编码端交付需求和明确版本，由 konden 团队独立验证，再把问题与证据交回原会话。</Text>
    <Text size="sm" c="dimmed">同一配置可以复用。编码会话自动领取身份，并从同事名单选择验证对象；用户无需取名或指定固定交接人。</Text>
    {created === null ? <Group justify="flex-start">
      <Button loading={creating.isLoading} disabled={connectionId === null || busy} onClick={() => void create()}>生成连接配置</Button>
    </Group> : <Stack gap="sm">
      <Text fw={600} size="sm">连接配置已生成</Text>
      <Text size="sm">请保存配置。凭据只在这次生成后显示，关闭设置后无法再次查看；遗失时请撤销并重新生成。</Text>
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
    {connectionId === null ? <Text size="sm" c="dimmed">正在连接，恢复后可生成或撤销配置。</Text> : null}
    <Divider />
    <Stack gap="sm">
      <Text fw={600} size="sm">已生成的连接配置</Text>
      {loadError !== undefined ? <Alert color="orange" role="alert">
        无法读取连接配置。<Button size="compact-xs" variant="subtle" onClick={() => void refetch()}>重试</Button>
      </Alert> : isLoading ? <Text size="sm" c="dimmed">正在读取…</Text>
        : connections.length === 0 ? <Text size="sm" c="dimmed">尚未生成连接配置。</Text> : null}
      {connections.map((item) => {
        const label = `连接 ${item.connection_id.slice(0, 8)}`;
        return <div key={item.connection_id}>
          <Group justify="space-between" wrap="nowrap">
            <Text size="sm">{label}</Text>
            <Button size="compact-xs" variant="subtle" color="red" disabled={connectionId === null || busy}
              aria-label={`撤销 ${label}`} onClick={() => setConfirmRevoke(item.connection_id)}>撤销</Button>
          </Group>
          {confirmRevoke !== item.connection_id ? null : <Stack gap="xs" mt="xs">
            <Text size="sm">撤销后，使用此配置的编码会话立即失去提交和读取能力，已有验证记录保留。</Text>
            <Group gap="xs" justify="flex-end">
              <Button size="compact-xs" variant="subtle" disabled={busy} onClick={() => setConfirmRevoke(null)}>取消</Button>
              <Button size="compact-xs" color="red" loading={revoking.isLoading} disabled={connectionId === null || busy}
                onClick={() => void revoke(item.connection_id)}>确认撤销</Button>
            </Group>
          </Stack>}
        </div>;
      })}
    </Stack>
  </Stack>;
}
