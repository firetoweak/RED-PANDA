import { Accordion, Alert, Button, Divider, Group, PasswordInput, Stack, Text, Textarea, TextInput } from "@mantine/core";
import { useClipboard } from "@mantine/hooks";
import { useState } from "react";

import type { CreatedExternalConnection } from "../../api/contracts";
import { useCreateExternalConnectionMutation, useGetExternalConnectionsQuery, useRevokeExternalConnectionMutation, useRenameExternalConnectionMutation } from "../../api/kondenApi";
import { useAppSelector } from "../../app/hooks";

export function ExternalAccess() {
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const { currentData: connections = [], isLoading, error: loadError, refetch } = useGetExternalConnectionsQuery(undefined, { pollingInterval: 5000 });
  const [createConnection, creating] = useCreateExternalConnectionMutation();
  const [revokeConnection, revoking] = useRevokeExternalConnectionMutation();
  const [renameConnection, renaming] = useRenameExternalConnectionMutation();
  const [name, setName] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [editName, setEditName] = useState("");
  const [created, setCreated] = useState<CreatedExternalConnection | null>(null);
  const [confirmRevoke, setConfirmRevoke] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copyTarget, setCopyTarget] = useState<"url" | "authorization" | "config" | null>(null);
  const clipboard = useClipboard();
  const busy = creating.isLoading || revoking.isLoading || renaming.isLoading;
  const config = created === null ? "" : [
    "[mcp_servers.konden]",
    `url = ${JSON.stringify(created.url)}`,
    "",
    "[mcp_servers.konden.http_headers]",
    `Authorization = ${JSON.stringify(`Bearer ${created.token}`)}`,
  ].join("\n");

  function copy(value: string, target: typeof copyTarget) {
    clipboard.reset();
    setCopyTarget(target);
    clipboard.copy(value);
  }

  async function create() {
    if (connectionId === null || busy) return;
    setError(null);
    clipboard.reset();
    const request = createConnection({ connectionId, name: name.trim() });
    try {
      setCreated(await request.unwrap());
      setName("");
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

  async function rename(externalId: string) {
    if (connectionId === null || busy) return;
    setError(null);
    try {
      await renameConnection({ connectionId, externalId, name: editName.trim() }).unwrap();
      setEditing(null);
    } catch (cause) {
      setError(typeof cause === "string" ? cause : "重命名失败，请重试。");
    }
  }

  return <Stack gap="md">
    <Text size="sm">在 Codex 中添加 Streamable HTTP 服务。</Text>
    {created === null ? <Stack gap="sm">
      <TextInput label="接入名称" description="用设备或用途区分，例如：笔记本 Codex、项目验证。可稍后修改。"
        placeholder="给这份连接配置起个名字" maxLength={80} value={name} onChange={(event) => setName(event.currentTarget.value)} />
      <Group justify="flex-start">
      <Button loading={creating.isLoading} disabled={connectionId === null || busy} onClick={() => void create()}>生成连接配置</Button>
    </Group>
    </Stack> : <Stack gap="sm">
      <Stack gap="xs">
        <TextInput label="URL" value={created.url} readOnly />
        <Group justify="flex-end">
          <Button size="compact-xs" variant="subtle" onClick={() => copy(created.url, "url")}>{clipboard.copied && copyTarget === "url" ? "已复制" : "复制 URL"}</Button>
        </Group>
      </Stack>
      <Stack gap="xs">
        <PasswordInput label="HTTP 请求头：Authorization" description="凭据仅显示一次" value={`Bearer ${created.token}`} readOnly
          visibilityToggleButtonProps={{ "aria-label": "显示或隐藏 Authorization 值" }} />
        <Group justify="flex-end">
          <Button size="compact-xs" variant="subtle" onClick={() => copy(`Bearer ${created.token}`, "authorization")}>{clipboard.copied && copyTarget === "authorization" ? "已复制" : "复制请求头值"}</Button>
        </Group>
      </Stack>
      <Accordion>
        <Accordion.Item value="config">
          <Accordion.Control>高级：手动配置 TOML</Accordion.Control>
          <Accordion.Panel>
            <Stack gap="sm">
              <Textarea label="Codex MCP 配置（TOML）" value={config} readOnly autosize minRows={5} maxRows={10}
                classNames={{ input: "external-connection-config" }} />
              <Group justify="flex-end">
                <Button size="compact-xs" variant="subtle" onClick={() => copy(config, "config")}>{clipboard.copied && copyTarget === "config" ? "已复制" : "复制 TOML"}</Button>
              </Group>
            </Stack>
          </Accordion.Panel>
        </Accordion.Item>
      </Accordion>
      <Group justify="flex-start">
        <Button variant="subtle" onClick={() => { setCreated(null); clipboard.reset(); }}>完成</Button>
      </Group>
      {clipboard.error === null ? null : <Alert color="orange" role="alert">复制失败，请选中对应字段手动复制。</Alert>}
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
        const label = item.name || `连接 ${item.connection_id.slice(0, 8)}`;
        const latest = item.activity.at(-1);
        return <div key={item.connection_id}>
          <Group justify="space-between" wrap="nowrap">
            <Stack gap={2} style={{ minWidth: 0, overflowWrap: "anywhere" }}>
              <Text size="sm" fw={600}>{label}</Text>
              <Text size="xs" c="dimmed">ID {item.connection_id.slice(0, 8)} · {latest ? `最近访问 ${new Date(latest.occurred_at).toLocaleString()}` : "尚无访问记录"}</Text>
            </Stack>
            <Button size="compact-xs" variant="subtle" disabled={connectionId === null || busy}
              aria-label={`重命名 ${label}`} onClick={() => { setEditing(item.connection_id); setEditName(item.name); }}>重命名</Button>
            <Button size="compact-xs" variant="subtle" color="red" disabled={connectionId === null || busy}
              aria-label={`撤销 ${label}`} onClick={() => setConfirmRevoke(item.connection_id)}>撤销</Button>
          </Group>
          {editing !== item.connection_id ? null : <Stack gap="xs" mt="xs">
            <TextInput label="修改接入名称" maxLength={80} value={editName} onChange={(event) => setEditName(event.currentTarget.value)}
              onKeyDown={(event) => { if (event.key === "Enter") void rename(item.connection_id); }} />
            <Group justify="flex-end" gap="xs">
              <Button size="compact-xs" variant="subtle" disabled={busy} onClick={() => setEditing(null)}>取消命名</Button>
              <Button size="compact-xs" loading={renaming.isLoading} disabled={connectionId === null || busy} onClick={() => void rename(item.connection_id)}>保存名称</Button>
            </Group>
          </Stack>}
          <Accordion variant="default" mt="xs">
            <Accordion.Item value="activity">
              <Accordion.Control>最近活动日志{item.activity.length ? `（${item.activity.length}）` : ""}</Accordion.Control>
              <Accordion.Panel>
                <Stack gap="sm">
                  <Text size="xs" c="dimmed">保留最近 30 次工具访问，每 5 秒刷新；访问记录不代表客户端持续在线。</Text>
                  {item.activity.length === 0 ? <Text size="sm" c="dimmed">尚无访问记录。请在 Codex 中添加配置并使用 konden 工具。</Text>
                    : [...item.activity].reverse().map((entry, index) => <Stack key={index} gap={2}>
                      <Text size="xs" c="dimmed">{new Date(entry.occurred_at).toLocaleString()}</Text>
                      <Text size="sm">{{ "tools/list": "查询工具列表", get_context: "登记 / 查询聊天", send_message: "发送消息", read_messages: "读取消息" }[entry.operation]} · {entry.result === "OK" ? "成功" : entry.result}</Text>
                      {entry.identity === null ? null : <Text size="xs" style={{ overflowWrap: "anywhere" }}>聊天 {entry.identity}</Text>}
                    </Stack>)}
                </Stack>
              </Accordion.Panel>
            </Accordion.Item>
          </Accordion>
          {confirmRevoke !== item.connection_id ? null : <Stack gap="xs" mt="xs">
            <Text size="sm">撤销后，使用此配置的外部会话立即失去收发能力，已有对话记录保留。</Text>
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
