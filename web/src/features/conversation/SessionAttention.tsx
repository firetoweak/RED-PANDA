import { Button, Group, Paper, Stack, Text } from "@mantine/core";

import type { InboxItem } from "../../api/contracts";
import { useAnswerAbsenceMutation, useGetInboxQuery, useRetryTurnMutation } from "../../api/redpandaApi";

/** 授权与控制提案由 PendingActions 展示；其余需要人介入的事项留在所属 Bot 的对话中。 */
export function SessionAttention({ sessionId, connectionId }: { sessionId: string; connectionId: string | null }) {
  const { data: inbox = [] } = useGetInboxQuery();
  const [answerAbsence, answering] = useAnswerAbsenceMutation();
  const [retryTurn, retrying] = useRetryTurnMutation();
  const items = inbox.filter((item): item is Extract<InboxItem, { kind: "limit" | "stopped" | "absence" }> => item.session_id === sessionId
    && item.kind !== "authorization" && item.kind !== "control");
  const disabled = connectionId === null || answering.isLoading || retrying.isLoading;
  if (items.length === 0) return null;

  return <Stack gap="xs" mb={8} aria-label="需要你处理的事项">
    {items.map((item) => <Paper key={item.kind === "limit" ? item.event_id : item.kind} p="sm" radius="md" withBorder>
      {item.kind === "limit" ? <Text className="pre-wrap" fz={13}>与 {item.recipient} 的交流已达上限。{item.text}</Text>
        : <Stack gap="xs">
          <Text className="pre-wrap" fz={13}>{item.summary}</Text>
          <Group justify="flex-end" gap="xs">
            {item.kind !== "absence" || item.phase !== "asked" ? null : <Button variant="subtle" color="gray" size="compact-sm" disabled={disabled}
              onClick={() => { if (connectionId !== null) void answerAbsence({ connectionId, sessionId, action: "stop" }); }}>停下</Button>}
            <Button variant="light" size="compact-sm" disabled={disabled}
              onClick={() => {
                if (connectionId === null) return;
                if (item.kind === "stopped") void retryTurn({ connectionId, sessionId });
                else void answerAbsence({ connectionId, sessionId, action: "continue" });
              }}>{item.kind === "absence" && item.phase === "paused" ? "恢复" : "继续"}</Button>
          </Group>
        </Stack>}
    </Paper>)}
    {answering.isError || retrying.isError ? <Text c="red" size="sm">操作没有完成，请重试。</Text> : null}
  </Stack>;
}
