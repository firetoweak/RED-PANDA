import {
  Alert, Badge, Button, Group, MultiSelect, Paper, ScrollArea, Stack, Text, Textarea, Tooltip,
} from "@mantine/core";
import { IconPlayerStop, IconSend } from "@tabler/icons-react";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import type { GroupMessage } from "../../api/contracts";
import {
  useGetAgentsQuery,
  useGetGroupQuery,
  usePostGroupMessageMutation,
  useStopGroupMutation,
} from "../../api/redpandaApi";
import { useAppSelector } from "../../app/hooks";
import { MarkdownMessage } from "../conversation/MarkdownMessage";
import { formatMessageTime } from "../conversation/messageTime";

function GroupMessageRow({ message, sessionOf }: {
  message: GroupMessage;
  sessionOf: (name: string) => string | undefined;
}) {
  const time = formatMessageTime(message.occurred_at);
  const session = message.sender === null ? undefined : sessionOf(message.sender);
  return (
    <Paper className={message.sender === null ? "group-message is-human" : "group-message"}
      p="sm" radius="md" withBorder>
      <Group gap="xs" justify="space-between" wrap="nowrap">
        <Group gap={6} wrap="nowrap">
          {message.sender === null ? (
            <Text fw={600} fz={13}>你</Text>
          ) : session === undefined ? (
            <Text fw={600} fz={13}>{message.sender}</Text>
          ) : (
            <Text component={Link} fw={600} fz={13} to={`/sessions/${encodeURIComponent(session)}`}>
              {message.sender}
            </Text>
          )}
          {message.mentioned_agents.map((name) => (
            <Badge key={name} size="xs" variant="light">@{name}</Badge>
          ))}
        </Group>
        <Tooltip label={time.full} openDelay={400}>
          <Text c="dimmed" fz={11}>{time.short}</Text>
        </Tooltip>
      </Group>
      <MarkdownMessage content={message.text} streaming={false} />
    </Paper>
  );
}

export function GroupChatPage() {
  const groupId = useParams().groupId!;
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const { data: group, error: loadError } = useGetGroupQuery(groupId);
  const { data: agents = [] } = useGetAgentsQuery();
  const [postMessage, posting] = usePostGroupMessageMutation();
  const [stopGroup, stopping] = useStopGroupMutation();
  const [text, setText] = useState("");
  const [mention, setMention] = useState<string[]>([]);
  const [failure, setFailure] = useState<string | null>(null);

  if (loadError !== undefined) {
    return <Text c="dimmed" p="lg">这个群不存在或已无法读取。</Text>;
  }
  if (group === undefined) {
    return null;
  }
  const sessionOf = (name: string) => agents.find(
    (agent) => agent.workspace_id === group.workspace_id && agent.name === name,
  )?.session_id;
  const canSend = connectionId !== null && text.trim() !== "" && !posting.isLoading;

  async function send() {
    if (!canSend || connectionId === null) {
      return;
    }
    setFailure(null);
    try {
      await postMessage({ connectionId, groupId, text, mention }).unwrap();
      setText("");
      setMention([]);
    } catch (cause) {
      setFailure(typeof cause === "string" ? cause : "发送没有完成");
    }
  }

  async function stop() {
    if (connectionId === null) {
      return;
    }
    setFailure(null);
    try {
      await stopGroup({ connectionId, groupId }).unwrap();
    } catch {
      setFailure("停止没有完成");
    }
  }

  return (
    <Stack className="group-chat" gap="sm" h="100%" maw={860} mx="auto" p="lg">
      <Group justify="space-between" wrap="nowrap">
        <Stack gap={2}>
          <Text fw={700} size="lg">{group.name}</Text>
          <Group gap={4}>
            {group.members.map((name) => (
              <Badge key={name} color="gray" size="sm" variant="light">{name}</Badge>
            ))}
          </Group>
        </Stack>
        <Button color="gray" disabled={!group.active || connectionId === null} loading={stopping.isLoading}
          leftSection={<IconPlayerStop size={15} />} onClick={() => void stop()} size="compact-sm" variant="light">
          停止本轮
        </Button>
      </Group>
      <ScrollArea className="group-chat-scroll" style={{ flex: 1 }} type="hover">
        <Stack gap="xs">
          {group.messages.length === 0 ? (
            <Text c="dimmed" fz={13}>还没有人发言。</Text>
          ) : group.messages.map((message) => (
            <GroupMessageRow key={message.message_id} message={message} sessionOf={sessionOf} />
          ))}
        </Stack>
      </ScrollArea>
      {group.calling.length === 0 ? null : (
        <Text c="dimmed" fz={12}>正在等 {group.calling.join("、")} 回应…</Text>
      )}
      {failure === null ? null : <Alert color="red">{failure}</Alert>}
      <Stack gap={6}>
        <MultiSelect
          aria-label="点名"
          clearable
          data={group.members}
          onChange={setMention}
          placeholder={mention.length === 0 ? "不点名则叫全体成员" : undefined}
          size="xs"
          value={mention}
        />
        <Group align="flex-end" gap="xs" wrap="nowrap">
          <Textarea
            aria-label="群消息"
            autosize
            maxRows={8}
            minRows={2}
            onChange={(event) => setText(event.currentTarget.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
                event.preventDefault();
                void send();
              }
            }}
            placeholder="在群里说点什么（Ctrl+Enter 发送）"
            style={{ flex: 1 }}
            value={text}
          />
          <Button aria-label="发送到群" disabled={!canSend} loading={posting.isLoading}
            onClick={() => void send()}>
            <IconSend size={16} />
          </Button>
        </Group>
      </Stack>
    </Stack>
  );
}
