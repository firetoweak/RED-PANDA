import { Alert, Button, Group, Loader, ScrollArea, Select, Text } from "@mantine/core";
import { IconPlugConnected } from "@tabler/icons-react";
import { useContext, useMemo } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import type { ExternalConversationMessage } from "../../api/contracts";
import { useGetExternalConversationQuery } from "../../api/kondenApi";
import { BotAvatar, ChatPresentation, MessageQuote, messageReference } from "../conversation/ChatPresentation";
import { MarkdownMessage } from "../conversation/MarkdownMessage";
import { formatMessageTime } from "../conversation/messageTime";
import { useMessageReading } from "../conversation/useMessageReading";
import { CodexContinuation } from "./CodexContinuation";

const EMPTY_MESSAGES: ExternalConversationMessage[] = [];

export function ExternalSessionPage() {
  const identity = useParams().identity!;
  const [search, setSearch] = useSearchParams();
  const bot = search.get("bot") ?? "all";
  return <ExternalConversationView key={`${identity}:${bot}`} identity={identity} bot={bot}
    onBot={(value) => setSearch(value === "all" ? {} : { bot: value })} />;
}

function ExternalConversationView({ identity, bot, onBot }: { identity: string; bot: string; onBot: (value: string) => void }) {
  const { data: conversation, error, refetch } = useGetExternalConversationQuery(identity);
  const presentation = useContext(ChatPresentation);
  const messages = useMemo(() => conversation?.messages.filter((message) => bot === "all" || message.bot_session_id === bot) ?? EMPTY_MESSAGES, [conversation, bot]);
  const reading = useMessageReading(`external:${identity}:${bot}`, messages);
  if (error !== undefined) return <div className="conversation-loading"><Alert color="orange">无法打开这个外部会话。<Button variant="subtle" size="compact-sm" onClick={() => void refetch()}>重试</Button></Alert></div>;
  if (conversation === undefined) return <div className="conversation-loading"><Loader size="sm" /><Text c="dimmed" size="sm">正在打开外部会话…</Text></div>;
  const { session } = conversation;
  const author = (message: ExternalConversationMessage) => message.direction === "received" ? session.name : message.bot_name;
  return <ChatPresentation value={{ ...presentation, names: [session.name, ...session.peers.map((peer) => peer.name)], onJump: reading.jump }}>
    <div className="session-page external-session-page">
      <div className="session-main">
        <header className="page-header session-header">
          <Group gap={10} wrap="nowrap" className="session-header-identity">
            <span className="agent-avatar agent-avatar-small" aria-hidden="true"><IconPlugConnected size={18} /></span>
            <div className="session-header-name">
              <Text fw={600} size="sm" truncate>{session.name}</Text>
              <Text c="dimmed" fz={11}>外部会话 · {session.active ? "接入有效" : "接入已撤销"}</Text>
            </div>
          </Group>
          <Text c="dimmed" fz={11} className="external-session-last-activity" title={formatMessageTime(session.last_activity_at).full}>最近活动 {formatMessageTime(session.last_activity_at).short}</Text>
        </header>
        <CodexContinuation identity={identity} active={session.active} value={conversation.codex} />
        {session.peers.length === 0 ? null : <div className="external-session-controls">
          <Select aria-label="查看与哪位 Bot 的对话" value={bot} allowDeselect={false}
            data={[{ value: "all", label: "全部 Bot 的对话" }, ...session.peers.map((peer) => ({ value: peer.session_id, label: peer.name + (peer.deleted ? "（已退出）" : "") }))]}
            onChange={(value) => { if (value !== null) onBot(value); }} size="xs" />
          <Text c="dimmed" size="xs">仅显示经 konden 交换的消息</Text>
        </div>}
        {session.active ? null : <Alert color="gray" className="external-session-revoked">接入已撤销，已有对话记录继续保留。</Alert>}
        <ScrollArea className="group-chat-scroll" type="hover" viewportRef={reading.viewport}
          onScrollPositionChange={({ y }) => reading.onScroll(y)}>
          <div className="group-transcript" role="region" aria-label="外部会话消息">
            {messages.length === 0 ? <div className="group-chat-empty">
              <span className="agent-avatar agent-details-avatar" aria-hidden="true"><IconPlugConnected size={28} /></span>
              <Text fw={500} size="lg">{!session.active ? "此会话尚无消息" : session.peers.length === 0 ? "外部会话已接入" : "这里还没有消息"}</Text>
              <Text c="dimmed" size="sm">在原生聊天中向同事发送消息，这里会保留双方的对话。</Text>
            </div> : messages.map((message, index) => {
              const source = messages.find((item) => item.message_id === message.reply_to);
              const time = formatMessageTime(message.occurred_at);
              return <div key={message.message_id} className="group-message-slot">
                {index > 0 && messages[index - 1].message_id === reading.unreadAfter ? <div className="unread-divider" role="separator">以下是未读消息</div> : null}
                <article className="group-message" data-message-id={message.message_id} aria-label={`${author(message)}的消息`}>
                  <div className="message group-message-row">
                    {message.direction === "received" ? <span className="agent-avatar" aria-hidden="true"><IconPlugConnected size={18} /></span> : <BotAvatar name={message.bot_name} sessionId={message.bot_session_id} />}
                    <div className="group-message-bubble" tabIndex={0}>
                      {source === undefined ? null : <MessageQuote quote={messageReference(source.message_id, author(source), source.text)} />}
                      <MarkdownMessage content={message.text} streaming={false} />
                    </div>
                  </div>
                  <div className="turn-details group-message-meta external-message-meta">
                    <Text size="xs">{message.direction === "received" ? `${session.name} → ` : ""}<Link to={`/sessions/${encodeURIComponent(message.bot_session_id)}`}>{message.bot_name}</Link>{message.direction === "sent" ? ` → ${session.name}` : ""}</Text>
                    <Text className="message-time" component="time" dateTime={message.occurred_at} title={time.full} c="dimmed" fz={11}>{time.short}</Text>
                  </div>
                </article>
              </div>;
            })}
          </div>
        </ScrollArea>
        <div className="external-session-footer">
          {reading.canReturn ? <Button className="jump-to-latest quote-return" variant="default" size="compact-sm" onClick={reading.returnToReading}>返回刚才的位置</Button>
            : reading.atBottom ? null : <Button className="jump-to-latest" variant="default" size="compact-sm" onClick={reading.latest}>{reading.pending > 0 ? `${reading.pending} 条新消息` : "回到最新"}</Button>}
          <Text c="dimmed" size="xs">需要调整任务时，可进入对应 Bot 的对话说明。</Text>
        </div>
      </div>
    </div>
  </ChatPresentation>;
}
