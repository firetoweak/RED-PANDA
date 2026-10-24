import { Alert, Button, Group, Modal, Stack, Text } from "@mantine/core";
import { useState } from "react";
import { useDeleteAgentMutation, useDissolveGroupMutation } from "../../api/kondenApi";
import { useAppSelector } from "../../app/hooks";

export type RemovalTarget = { kind: "agent" | "group"; id: string; name: string };

export function RemoveConversationModal({ target, onClose, onRemoved }: {
  target: RemovalTarget; onClose: () => void; onRemoved: () => void;
}) {
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const [deleteAgent, deleting] = useDeleteAgentMutation();
  const [dissolveGroup, dissolving] = useDissolveGroupMutation();
  const [error, setError] = useState<string | null>(null);
  const action = target.kind === "agent" ? "删除 Bot" : "解散群聊";
  const busy = deleting.isLoading || dissolving.isLoading;
  async function remove() {
    if (connectionId === null || busy) return;
    setError(null);
    try {
      if (target.kind === "agent") await deleteAgent({ connectionId, sessionId: target.id }).unwrap();
      else await dissolveGroup({ connectionId, groupId: target.id }).unwrap();
      onRemoved();
    } catch (cause) {
      setError(typeof cause === "string" ? cause : `${action}失败，请重试。`);
    }
  }
  return <Modal opened title={`${action}「${target.name}」？`} onClose={onClose}
    closeOnClickOutside={!busy} closeOnEscape={!busy} withCloseButton={!busy}>
    <Stack gap="md">
      <Text size="sm">{target.kind === "agent"
        ? "它将停止执行、子任务和定时唤醒，退出所有群聊，并从名册移除。聊天记录、个人目录和 Git 成果保留。"
        : "群聊将从列表移除，不再接收发言或叫成员回应。聊天记录保留，成员 Bot 继续正常工作。"}</Text>
      <Text size="sm" c="dimmed">此操作无法撤销，保留的聊天记录可通过原链接只读查看。</Text>
      {error === null ? null : <Alert color="red" role="alert">{error}</Alert>}
      <Group justify="flex-end">
        <Button data-autofocus variant="default" disabled={busy} onClick={onClose}>取消</Button>
        <Button color="red" loading={busy} disabled={connectionId === null} onClick={() => void remove()}>{action}</Button>
      </Group>
    </Stack>
  </Modal>;
}
