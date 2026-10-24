import {
  ActionIcon, Alert, Button, Drawer, Group, Loader, ScrollArea, Text, Tooltip,
} from "@mantine/core";
import { IconLayoutSidebarRightCollapse, IconLayoutSidebarRightExpand, IconUsersGroup } from "@tabler/icons-react";
import { useState } from "react";
import { z } from "zod";
import { useDidUpdate, useMediaQuery } from "@mantine/hooks";
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
import { BotAvatar, ChatPresentation, useIdentityColor, messageReference, MessageQuote, MessageMenu, type MessageReference } from "../conversation/ChatPresentation";
import { useGroupReading } from "./useGroupReading";
import { createClientId } from "../conversation/clientId";

function GroupMessageRow({ message, source, groupId, groupName, sessionOf }: {
  message: GroupMessage;
  source?: GroupMessage;
  groupId: string;
  groupName: string;
  sessionOf: (name: string) => string | undefined;
}) {
  const time = formatMessageTime(message.occurred_at);
  const identityColor = useIdentityColor();
  const session = message.sender === null ? undefined : sessionOf(message.sender);
  return (
    <article data-message-id={message.message_id} className={message.sender === null ? "group-message is-human" : "group-message"}
      aria-label={`${message.sender ?? "你"}的群消息`}>
      <div className="message group-message-row">
        {message.sender === null ? <span className="agent-avatar human-avatar" aria-hidden="true">你</span> : <BotAvatar name={message.sender} sessionId={session} />}
        <MessageMenu id={message.message_id} author={message.sender ?? "你"} text={message.text}>
          <div className="group-message-bubble" tabIndex={0}>
            {source === undefined ? null : <MessageQuote quote={messageReference(source.message_id, source.sender ?? "你", source.text)} />}
            {message.mentioned_agents.every((name) => message.text.includes(`@${name}`)) ? null : <Group gap={4} mb={6}>
              {message.mentioned_agents.filter((name) => !message.text.includes(`@${name}`)).map((name) => <span key={name} className="message-mention" data-identity={identityColor(name)}>@{name}</span>)}
            </Group>}
            <MarkdownMessage content={message.text} streaming={false} />
          </div>
        </MessageMenu>
      </div>
      <div className="turn-details group-message-meta">
        {message.sender === null ? null : session === undefined ? (
          <Text data-identity={identityColor(message.sender)} fw={600} fz={12}>{message.sender}</Text>
        ) : (
          <Text component={Link} data-identity={identityColor(message.sender)} fw={600} fz={12} to={`/sessions/${encodeURIComponent(session)}`} state={{ fromGroup: { id: groupId, name: groupName } }}>
            {message.sender}
          </Text>
        )}
        <Text className="message-time" component="time" dateTime={message.occurred_at} title={`发送于 ${time.full}`} c="dimmed" fz={11}>{time.short}</Text>
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
  const [detailsOpened, setDetailsOpened] = useState(false);
  const [quote, setQuote] = useState<MessageReference | null>(null);
  const narrow = useMediaQuery("(max-width: 73.75em)");
  useDidUpdate(() => setDetailsOpened(false), [narrow]);
  const reading = useGroupReading(groupId, group?.messages);

  if (loadError !== undefined) {
    return <Text c="dimmed" p="lg">这个群不存在或已无法读取。</Text>;
  }
  if (group === undefined) {
    return <div className="conversation-loading"><Loader size="sm" /><Text c="dimmed" size="sm">正在打开群聊…</Text></div>;
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
      const key = `redpanda.groupPending.${groupId}`;
      const saved = localStorage.getItem(key);
      const previous = saved === null ? null : z.object({ content: z.string(), deliveryId: z.string().min(1) }).parse(JSON.parse(saved));
      const content = JSON.stringify([text, mention]);
      const pending = previous?.content === content ? previous : { content, deliveryId: `web-${createClientId()}` };
      localStorage.setItem(key, JSON.stringify(pending));
      await postMessage({ connectionId, groupId, text, mention, deliveryId: pending.deliveryId }).unwrap();
      localStorage.removeItem(key);
      reading.latest();
      return true;
    } catch (cause) {
      setFailure(typeof cause === "string" ? cause : "发送没有完成");
      return false;
    }
  }

  const details = <>
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
              <BotAvatar name={name} sessionId={sessionOf(name)} />
              <div className="group-member-identity"><Text fz={13} fw={500} truncate>{name}</Text></div>
              {agent === undefined ? null : <AgentStatus agent={agent} />}
            </>;
            return agent === undefined ? <div className="group-member" key={name}>{identity}</div> : (
              <Link className="group-member" key={name} aria-label={`查看 ${name} 的会话`}
                to={`/sessions/${encodeURIComponent(agent.session_id)}`} state={{ fromGroup: { id: groupId, name: group.name } }}>{identity}</Link>
            );
          })}</div>
        </div>
      </>;
  return (
    <ChatPresentation value={{ names: group.members, quote, onQuote: setQuote, onJump: reading.jump }}>
    <div className="session-page group-chat">
      <div className="session-main">
        <header className="page-header session-header">
          <Group gap={10} wrap="nowrap" className="session-header-identity">
            <span className="agent-avatar agent-avatar-small"><IconUsersGroup size={18} /></span>
            <div className="session-header-name">
              <Text fw={600} size="sm" truncate>{group.name}</Text>
              <Text c="dimmed" fz={11}>{group.members.length} 位 Bot · {group.messages.length} 条消息</Text>
            </div>
          </Group>
          <div className="group-header-members" aria-label="群成员">{group.members.map((name) => <BotAvatar key={name} name={name} />)}</div>
          <Tooltip label={detailsOpened ? "收起群资料" : "显示群资料"}>
            <ActionIcon className="agent-details-toggle" aria-label={detailsOpened ? "收起群资料" : "显示群资料"}
              aria-expanded={detailsOpened} onClick={() => setDetailsOpened((opened) => !opened)} variant="subtle" color="gray">
              {detailsOpened ? <IconLayoutSidebarRightCollapse size={18} /> : <IconLayoutSidebarRightExpand size={18} />}
            </ActionIcon>
          </Tooltip>
        </header>
        <ScrollArea className="group-chat-scroll" viewportRef={reading.viewport} type="hover"
          onScrollPositionChange={({ y }) => reading.onScroll(y)}>
          <div className="group-transcript" role="region" aria-label="聊天消息">
            {group.messages.length === 0 ? (
              <div className="group-chat-empty">
                <span className="agent-avatar agent-details-avatar"><IconUsersGroup size={30} /></span>
                <Text fw={500} size="lg">和 {group.members.join("、")} 一起开始</Text>
                <Text c="dimmed" size="sm">在这里分配任务、同步进展，或点名请某位成员回应。</Text>
              </div>
            ) : group.messages.map((message, index) => <div className="group-message-slot" key={message.message_id}>
              {index > 0 && group.messages[index - 1].message_id === reading.unreadAfter ? <div className="unread-divider" role="separator">以下是未读消息</div> : null}
              <GroupMessageRow message={message} source={group.messages.find((item) => item.message_id === message.reply_to)} groupId={groupId} groupName={group.name} sessionOf={sessionOf} />
            </div>)}
          </div>
        </ScrollArea>
        <div className="group-composer-dock">
          {reading.canReturn ? <Button className="jump-to-latest quote-return" variant="default" size="compact-sm" onClick={reading.returnToReading}>返回刚才的位置</Button> : reading.atBottom ? null : <Button className="jump-to-latest" variant="default" size="compact-sm" onClick={reading.latest}>{reading.pending > 0 ? `${reading.pending} 条新消息` : "回到最新"} ↓</Button>}
          <div className="group-composer-status" aria-live="polite">
            {group.calling.length === 0 ? null : (
              <Text c="dimmed" fz={12}>正在通知 {group.calling.join("、")}…</Text>
            )}
          </div>
          {failure === null ? null : <Alert color="red">{failure}</Alert>}
          {group.dissolved ? <Text size="sm" c="dimmed" ta="center" py="md">群聊已解散，此处仅保留聊天记录。</Text>
            : group.members.length === 0 ? <Text size="sm" c="dimmed" ta="center" py="md">群里已没有 Bot，可从侧栏菜单解散群聊。</Text>
            : <GroupComposer groupId={groupId} groupName={group.name} members={group.members} connected={connectionId !== null}
              posting={posting.isLoading} onSend={send} />}
        </div>
      </div>
      {narrow ? <Drawer opened={detailsOpened} onClose={() => setDetailsOpened(false)} title="群资料" position="right" size="min(292px, 85vw)" classNames={{ header: "details-heading", body: "details-body" }} closeButtonProps={{ "aria-label": "关闭群资料" }}>{details}</Drawer>
        : !detailsOpened ? null : <aside className="agent-details" aria-label="群资料"><header className="details-heading"><Text fw={600} fz={12}>群资料</Text><ActionIcon aria-label="关闭群资料" variant="subtle" color="gray" onClick={() => setDetailsOpened(false)}><IconLayoutSidebarRightCollapse size={17} /></ActionIcon></header>{details}</aside>}
    </div>
    </ChatPresentation>
  );
}
