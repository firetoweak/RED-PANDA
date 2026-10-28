import { Alert, Button, Group, Stack, Text, TextInput } from "@mantine/core";
import { IconChevronDown, IconChevronUp } from "@tabler/icons-react";
import { useEffect, useId, useState } from "react";

import type { ExternalConversation } from "../../api/contracts";
import { useRetryExternalCodexMutation, useSetExternalCodexMutation } from "../../api/kondenApi";
import { useAppSelector } from "../../app/hooks";

const labels = {
  disabled: "未启用", ready: "已启用，等待新消息", pending: "有消息尚未提醒",
  queued: "提示已入队", failed: "提醒失败", unknown: "提醒结果未确认",
};

export function CodexContinuation({ identity, active, value }: {
  identity: string; active: boolean; value: ExternalConversation["codex"];
}) {
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const [setCodex, setting] = useSetExternalCodexMutation();
  const [retry, retrying] = useRetryExternalCodexMutation();
  const [opened, setOpened] = useState(false);
  const [thread, setThread] = useState(value.thread_id ?? "");
  const [executable, setExecutable] = useState(value.executable);
  const [error, setError] = useState<string | null>(null);
  const panelId = useId();
  const busy = setting.isLoading || retrying.isLoading;
  const enabled = value.thread_id !== null;
  const needsRetry = value.status === "failed" || value.status === "unknown" || value.status === "pending";

  useEffect(() => {
    if (!opened) {
      setThread(value.thread_id ?? "");
      setExecutable(value.executable);
    }
  }, [opened, value.thread_id, value.executable]);

  async function change(target: { thread_id: string; executable: string } | null) {
    if (connectionId === null || busy) return;
    setError(null);
    try {
      await setCodex({ connectionId, identity, target }).unwrap();
      setOpened(false);
    } catch (cause) {
      setError(typeof cause === "string" ? cause : "设置未保存，请检查连接后重试。");
    }
  }

  async function remind() {
    if (connectionId === null || busy) return;
    setError(null);
    try {
      await retry({ connectionId, identity }).unwrap();
    } catch (cause) {
      setError(typeof cause === "string" ? cause : "提醒未完成，请检查连接后重试。");
    }
  }

  if (!active && !enabled) return null;
  return <Stack gap="sm" className="external-codex">
    <Group gap="sm" justify="space-between">
      <Group gap="xs">
        <Button variant="subtle" size="compact-sm" color="gray" aria-expanded={opened} aria-controls={panelId}
          rightSection={opened ? <IconChevronUp size={14} /> : <IconChevronDown size={14} />}
          onClick={() => { setOpened(!opened); setError(null); }}>Codex 自动接续</Button>
        <Text size="xs" c={needsRetry ? undefined : "dimmed"} role="status" aria-label="Codex 自动接续状态">{labels[value.status]}</Text>
      </Group>
      {active && enabled && needsRetry ? <Button size="compact-sm" variant="light" loading={retrying.isLoading}
        disabled={connectionId === null || busy} onClick={() => void remind()}>重试提醒</Button> : null}
    </Group>
    {value.detail === null ? null : <Text size="xs" c={needsRetry ? undefined : "dimmed"} className="external-codex-detail">{value.detail}</Text>}
    {opened ? <form id={panelId} className="external-codex-form" onSubmit={(event) => {
      event.preventDefault();
      const threadId = thread.trim().toLowerCase();
      if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(threadId)) {
        setError("请填写 Codex 聊天 ID（UUID），可让目标聊天读取自身的 CODEX_THREAD_ID。");
        return;
      }
      void change({ thread_id: threadId, executable: executable.trim() });
    }}>
      <Stack gap="sm">
        <Text size="sm">Bot 发来消息时，提醒对应的 Codex 聊天继续处理。消息内容仍通过 MCP 读取，并遵守已有授权。</Text>
        <Text size="xs" c="dimmed">适用于同一台电脑上已加载的聊天。当前已验证空闲聊天，不保证打开应用或恢复未加载的聊天。</Text>
        <TextInput label="Codex 聊天 ID" description="可让目标聊天读取自身的 CODEX_THREAD_ID。每个聊天分别绑定。"
          value={thread} onChange={(event) => setThread(event.currentTarget.value)} required disabled={!active || busy} />
        <TextInput label="Codex 程序" description="本机 Codex 可执行文件的完整路径，或 PATH 中的 codex 命令。"
          value={executable} onChange={(event) => setExecutable(event.currentTarget.value)} required disabled={!active || busy} />
        {connectionId === null ? <Text size="xs" c="dimmed">正在重新连接，连接恢复后可以保存。</Text> : null}
        <Group gap="xs" justify="flex-end">
          {enabled ? <Button variant="subtle" color="gray" disabled={connectionId === null || busy}
            onClick={() => void change(null)}>停用自动接续</Button> : null}
          <Button variant="default" disabled={busy} onClick={() => setOpened(false)}>取消</Button>
          <Button type="submit" loading={setting.isLoading} disabled={!active || connectionId === null || busy || !thread.trim() || !executable.trim()}>
            {enabled ? "保存设置" : "保存并启用"}
          </Button>
        </Group>
      </Stack>
    </form> : null}
    {error === null ? null : <Alert color="red" role="alert">{error}</Alert>}
  </Stack>;
}
