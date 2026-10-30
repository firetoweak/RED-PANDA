import { Alert, Button, Group, Loader, ScrollArea, Select, Text } from "@mantine/core";
import { IconPlugConnected } from "@tabler/icons-react";
import { Fragment, useContext, useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import type { ExternalConversationMessage } from "../../api/contracts";
import { useGetAgentsQuery, useGetExternalConversationQuery, usePostExternalMessageMutation } from "../../api/kondenApi";
import { useAppSelector } from "../../app/hooks";
import { BotAvatar, ChatPresentation, MessageMenu, MessageQuote, messageReference, type MessageReference } from "../conversation/ChatPresentation";
import { MarkdownMessage } from "../conversation/MarkdownMessage";
import { formatMessageTime } from "../conversation/messageTime";
import { useMessageReading } from "../conversation/useMessageReading";
import { CodexContinuation } from "./CodexContinuation";
import { ExternalComposer } from "./ExternalComposer";
import { sessionStatus } from "./sessionStatus";

const EMPTY_MESSAGES: ExternalConversationMessage[] = [];

export function ExternalSessionPage() {
  const identity = useParams().identity!;
  const [search, setSearch] = useSearchParams();
  const bot = search.get("bot");
  return <ExternalConversationView key={`${identity}:${bot}`} identity={identity} bot={bot}
    onBot={(value) => setSearch({ bot: value }, { replace: bot === null })} />;
}

function ExternalConversationView({ identity, bot, onBot }: { identity: string; bot: string | null; onBot: (value: string) => void }) {
  const { data: conversation, error, refetch } = useGetExternalConversationQuery(identity);
  const { data: agents = [] } = useGetAgentsQuery();
  const [postMessage, posting] = usePostExternalMessageMutation();
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const [failure, setFailure] = useState<string | null>(null);
  const [quote, setQuote] = useState<MessageReference | null>(null);
  const presentation = useContext(ChatPresentation);
  const peers = useMemo(() => {
    const known = new Map(conversation?.session.peers.map((peer) => [peer.session_id, peer]));
    for (const agent of agents) known.set(agent.session_id, { name: agent.name, session_id: agent.session_id, deleted: false });
    return [...known.values()];
  }, [conversation, agents]);
  const selectedId = bot ?? (conversation?.session.peers.length === 1 ? conversation.session.peers[0].session_id
    : peers.length === 1 ? peers[0].session_id : null);
  const selected = peers.find((peer) => peer.session_id === selectedId);
  useEffect(() => { if (bot === null && selectedId !== null) onBot(selectedId); }, [bot, selectedId, onBot]);
  const messages = useMemo(() => conversation?.messages.filter((message) => message.bot_session_id === selectedId) ?? EMPTY_MESSAGES, [conversation, selectedId]);
  const byId = useMemo(() => new Map(messages.map((message) => [message.message_id, message])), [messages]);
  const reading = useMessageReading(`external:${identity}:${selectedId}`, messages);
  if (error !== undefined) return <div className="conversation-loading"><Alert color="orange">无法打开这个外部会话。<Button variant="subtle" size="compact-sm" onClick={() => void refetch()}>重试</Button></Alert></div>;
  if (conversation === undefined) return <div className="conversation-loading"><Loader size="sm" /><Text c="dimmed" size="sm">正在打开外部会话…</Text></div>;
  const { session } = conversation;
  const author = (message: ExternalConversationMessage) => message.sender === "user" ? "你" : message.sender === "codex" ? session.name : message.bot_name;
  const participant = (role: "bot" | "codex" | "user", message: ExternalConversationMessage) => role === "bot"
    ? <Link to={`/sessions/${encodeURIComponent(message.bot_session_id)}`}>{message.bot_name}</Link> : role === "codex" ? session.name : "你";
  return <ChatPresentation value={{ ...presentation, names: [session.name, "你", ...peers.map((peer) => peer.name)], quote, onQuote: setQuote, onJump: reading.jump }}>
    <div className="session-page external-session-page">
      <div className="session-main">
        <header className="page-header session-header">
          <Group gap={10} wrap="nowrap" className="session-header-identity">
            <span className="agent-avatar agent-avatar-small" aria-hidden="true"><IconPlugConnected size={18} /></span>
            <div className="session-header-name">
              <Text fw={600} size="sm" truncate>{session.name}</Text>
              <Text c="dimmed" fz={11}>Codex · {sessionStatus(session)}</Text>
            </div>
          </Group>
          {messages.length === 0 ? null : <Text c="dimmed" fz={11} className="external-session-last-activity"
            title={formatMessageTime(messages[messages.length - 1].occurred_at).full}>最近消息 {formatMessageTime(messages[messages.length - 1].occurred_at).short}</Text>}
        </header>
        <CodexContinuation identity={identity} active={session.active} value={conversation.codex} />
        <div className="external-session-controls">
          <Select aria-label="三方对话中的 Bot" placeholder={peers.length === 0 ? "暂无可用 Bot" : "选择一位 Bot"}
            value={selected?.session_id ?? null} allowDeselect={false} data={peers.map((peer) => ({ value: peer.session_id, label: peer.name + (peer.deleted ? "（已退出）" : "") }))}
            onChange={(value) => { if (value !== null) onBot(value); }} size="xs" />
          <Text c="dimmed" size="xs">{selected === undefined ? "选择后，你与这个 Codex 聊天、Bot 在此交流" : `你 · Codex · ${selected.name}`}</Text>
        </div>
        {session.active ? null : <Alert color="gray" className="external-session-revoked">接入已撤销，已有对话记录继续保留。</Alert>}
        {selected?.deleted ? <Alert color="gray" className="external-session-revoked">这位 Bot 已退出，已有对话记录继续保留。</Alert> : null}
        <ScrollArea className="group-chat-scroll" type="hover" viewportRef={reading.viewport} onScrollPositionChange={({ y }) => reading.onScroll(y)}>
          <div className="group-transcript" role="region" aria-label="外部会话消息">
            {messages.length === 0 ? <div className="group-chat-empty">
              <span className="agent-avatar agent-details-avatar" aria-hidden="true"><IconPlugConnected size={28} /></span>
              <Text fw={500} size="lg">{!session.active ? "此会话尚无消息" : selected === undefined ? "选择一位 Bot，开始三方对话" : "三方对话已准备好"}</Text>
              <Text c="dimmed" size="sm">{selected === undefined ? "每段对话固定一个 Codex 聊天、一位 Bot 和你。" : "你可以在下方发言；这里只保留三方明确交换的消息。"}</Text>
            </div> : messages.map((message, index) => {
              const source = message.reply_to === null ? undefined : byId.get(message.reply_to);
              const time = formatMessageTime(message.occurred_at);
              return <div key={message.message_id} className="group-message-slot">
                {index > 0 && messages[index - 1].message_id === reading.unreadAfter ? <div className="unread-divider" role="separator">以下是未读消息</div> : null}
                <article className={message.sender === "user" ? "group-message is-human" : "group-message"} data-message-id={message.message_id} aria-label={`${author(message)}的消息`}>
                  <div className="message group-message-row">
                    {message.sender === "user" ? <span className="agent-avatar human-avatar" aria-hidden="true">你</span>
                      : message.sender === "codex" ? <span className="agent-avatar" aria-hidden="true"><IconPlugConnected size={18} /></span>
                      : <BotAvatar name={message.bot_name} sessionId={message.bot_session_id} />}
                    <MessageMenu id={message.message_id} author={author(message)} text={message.text}>
                      <div className="group-message-bubble" tabIndex={0}>
                        {source === undefined ? null : <MessageQuote quote={messageReference(source.message_id, author(source), source.text)} />}
                        <MarkdownMessage content={message.text} streaming={false} />
                      </div>
                    </MessageMenu>
                  </div>
                  <div className="turn-details group-message-meta external-message-meta">
                    <Text size="xs">{participant(message.sender, message)} → {message.recipients.map((role, i) => <Fragment key={role}>{i === 0 ? "" : "、"}{participant(role, message)}</Fragment>)}</Text>
                    <Text className="message-time" component="time" dateTime={message.occurred_at} title={time.full} c="dimmed" fz={11}>{time.short}</Text>
                  </div>
                </article>
              </div>;
            })}
          </div>
        </ScrollArea>
        <div className="group-composer-dock">
          {reading.canReturn ? <Button className="jump-to-latest quote-return" variant="default" size="compact-sm" onClick={reading.returnToReading}>返回刚才的位置</Button>
            : reading.atBottom ? null : <Button className="jump-to-latest" variant="default" size="compact-sm" onClick={reading.latest}>{reading.pending > 0 ? `${reading.pending} 条新消息` : "回到最新"}</Button>}
          {failure === null ? null : <Alert color="red" role="alert" mb="sm">{failure}</Alert>}
          {selected === undefined ? <Text size="xs" c="dimmed" ta="center">{peers.length === 0 ? "先创建一位 Bot，再选择对话对象。" : "请先选择这段对话中的 Bot。"}</Text>
            : <ExternalComposer key={`${identity}:${selected.session_id}`} identity={identity} botId={selected.session_id} botName={selected.name}
              connected={session.active && !selected.deleted && connectionId !== null} posting={posting.isLoading}
              codexAutomatic={conversation.codex.status !== "disabled"}
              onSend={async (text, target, deliveryId, replyTo) => {
                if (connectionId === null) return false;
                setFailure(null);
                try {
                  await postMessage({ connectionId, identity, botSessionId: selected.session_id, text, target, deliveryId, replyTo }).unwrap();
                  reading.latest();
                  return true;
                } catch (cause) {
                  setFailure(typeof cause === "string" ? cause : "发送未完成，请重试；原消息编号会保留。");
                  return false;
                }
              }} />}
        </div>
      </div>
    </div>
  </ChatPresentation>;
}
