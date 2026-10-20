import {
  ActionIcon, Alert, Badge, Group, ScrollArea, Text, Tooltip,
} from "@mantine/core";
import { IconLayoutSidebarRightCollapse, IconLayoutSidebarRightExpand, IconUsersGroup } from "@tabler/icons-react";
import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import type { GroupMessage } from "../../api/contracts";
import {
  useGetAgentsQuery,
  useGetGroupQuery,
  usePostGroupMessageMutation,
} from "../../api/redpandaApi";
import { useAppSelector } from "../../app/hooks";
import { MarkdownMessage } from "../conversation/MarkdownMessage";
import { formatMessageTime } from "../conversation/messageTime";
import { AgentStatus } from "../sessions/AgentStatus";
import { GroupComposer } from "./GroupComposer";

function GroupMessageRow({ message, sessionOf }: {
  message: GroupMessage;
  sessionOf: (name: string) => string | undefined;
}) {
  const time = formatMessageTime(message.occurred_at);
  const session = message.sender === null ? undefined : sessionOf(message.sender);
  return (
    <article className={message.sender === null ? "group-message is-human" : "group-message"}
      aria-label={`${message.sender ?? "你"}的群消息`}>
      <Group className="group-message-meta" gap={6}>
        {message.sender === null ? (
          <Text fw={600} fz={13}>你</Text>
        ) : session === undefined ? (
          <Text fw={600} fz={13}>{message.sender}</Text>
        ) : (
          <Text component={Link} fw={600} fz={13} to={`/sessions/${encodeURIComponent(session)}`}>
            {message.sender}
          </Text>
        )}
        <Tooltip label={time.full} openDelay={400}>
          <Text c="dimmed" fz={11}>{time.short}</Text>
        </Tooltip>
      </Group>
      <div className="group-message-bubble">
        {message.mentioned_agents.length === 0 ? null : <Group gap={4} mb={6}>
          {message.mentioned_agents.map((name) => <Badge key={name} size="xs" color="gray" variant="light">@{name}</Badge>)}
        </Group>}
        <MarkdownMessage content={message.text} streaming={false} />
      </div>
    </article>
  );
}

export function GroupChatPage() {
  const groupId = useParams().groupId!;
  return <GroupConversation key={groupId} groupId={groupId} />;
}

function GroupConversation({ groupId }: { groupId: string; }) {
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const { data: group, error: loadError } = useGetGroupQuery(groupId);
  const { data: agents = [] } = useGetAgentsQuery();
  const [postMessage, posting] = usePostGroupMessageMutation();
  const [failure, setFailure] = useState<string | null>(null);
  const [detailsOpened, setDetailsOpened] = useState(true);
  const viewport = useRef<HTMLDivElement>(null);
  const followMessages = useRef(true);

  useEffect(() => {
    const element = viewport.current;
    if (element !== null && followMessages.current) element.scrollTop = element.scrollHeight;
  }, [group?.messages.length, group?.calling.join(",")]);

  if (loadError !== undefined) {
    return <Text c="dimmed" p="lg">这个群不存在或已无法读取。</Text>;
  }
  if (group === undefined) {
    return null;
  }
  const memberOf = (name: string) => agents.find(
    (agent) => agent.name === name,
  );
  const sessionOf = (name: string) => memberOf(name)?.session_id;


  async function send(text: string, mention: string[]) {
    if (connectionId === null || posting.isLoading) {
      return false;
    }
    setFailure(null);
    try {
      await postMessage({ connectionId, groupId, text, mention }).unwrap();
      followMessages.current = true;
      const element = viewport.current;
      if (element !== null) element.scrollTop = element.scrollHeight;
      return true;
    } catch (cause) {
      setFailure(typeof cause === "string" ? cause : "发送没有完成");
      return false;
    }
  }

  return (
    <div className="session-page group-chat">
      <div className="session-main">
        <header className="page-header session-header">
          <Group gap={10} wrap="nowrap" className="session-header-identity">
            <span className="agent-avatar agent-avatar-small"><IconUsersGroup size={18} /></span>
            <div className="session-header-name">
              <Text fw={600} size="sm" truncate>{group.name}</Text>
              <Text c="dimmed" fz={11}>{group.members.length} 位成员 · 群聊</Text>
            </div>
          </Group>
          <Tooltip label={detailsOpened ? "收起群资料" : "显示群资料"}>
            <ActionIcon className="agent-details-toggle" aria-label={detailsOpened ? "收起群资料" : "显示群资料"}
              aria-expanded={detailsOpened} onClick={() => setDetailsOpened((opened) => !opened)} variant="subtle" color="gray">
              {detailsOpened ? <IconLayoutSidebarRightCollapse size={18} /> : <IconLayoutSidebarRightExpand size={18} />}
            </ActionIcon>
          </Tooltip>
        </header>
        <ScrollArea className="group-chat-scroll" viewportRef={viewport} type="hover"
          onScrollPositionChange={({ y }) => {
            const element = viewport.current;
            if (element !== null) followMessages.current = element.scrollHeight - element.clientHeight - y < 80;
          }}>
          <div className="group-transcript">
            {group.messages.length === 0 ? (
              <div className="group-chat-empty">
                <span className="agent-avatar agent-details-avatar"><IconUsersGroup size={30} /></span>
                <Text fw={500} size="lg">和 {group.members.join("、")} 一起开始</Text>
                <Text c="dimmed" size="sm">在这里分配任务、同步进展，或点名请某位成员回应。</Text>
              </div>
            ) : group.messages.map((message) => (
              <GroupMessageRow key={message.message_id} message={message} sessionOf={sessionOf} />
            ))}
          </div>
        </ScrollArea>
        <div className="group-composer-dock">
          <div className="group-composer-status" aria-live="polite">
            {group.calling.length === 0 ? null : (
              <Text c="dimmed" fz={12}>正在等 {group.calling.join("、")} 回应…</Text>
            )}
          </div>
          {failure === null ? null : <Alert color="red">{failure}</Alert>}
          <GroupComposer groupName={group.name} members={group.members} connected={connectionId !== null}
            posting={posting.isLoading} onSend={send} />
          <div className="composer-help">Enter 发送 · Shift+Enter 换行 · 不点名则通知全体成员</div>
        </div>
      </div>
      {!detailsOpened ? null : <aside className="agent-details" aria-label="群资料">
        <div className="agent-details-profile">
          <span className="agent-avatar agent-details-avatar"><IconUsersGroup size={30} /></span>
          <Text fw={600} size="md">{group.name}</Text>
          <Text c="dimmed" fz={12}>{group.members.length} 位成员</Text>
        </div>
        <div className="agent-details-section">
          <Text className="agent-details-label" size="xs" c="dimmed">群成员</Text>
          <div className="group-member-list">{group.members.map((name) => {
            const agent = memberOf(name);
            const identity = <>
              <span className="agent-avatar">{name.slice(0, 1)}</span>
              <div className="group-member-identity"><Text fz={13} fw={500} truncate>{name}</Text>
                <Text fz={11} c="dimmed" lineClamp={2}>{agent?.direction ?? "尚未设置负责方向"}</Text></div>
              {agent === undefined ? null : <AgentStatus agent={agent} compact />}
            </>;
            return agent === undefined ? <div className="group-member" key={name}>{identity}</div> : (
              <Link className="group-member" key={name} aria-label={`查看 ${name} 的会话`}
                to={`/sessions/${encodeURIComponent(agent.session_id)}`}>{identity}</Link>
            );
          })}</div>
        </div>
      </aside>}
    </div>
  );
}
