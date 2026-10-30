import { Modal, UnstyledButton } from "@mantine/core";
import { IconArrowDownLeft, IconArrowUpRight, IconExternalLink, IconMessages } from "@tabler/icons-react";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { useGetAgentsQuery } from "../../api/kondenApi";
import { BotAvatar, useIdentityColor } from "./ChatPresentation";
import { formatMessageTime } from "./messageTime";
import type { TimelineCommunication } from "./timelineTurns";
import { MarkdownMessage } from "./MarkdownMessage";

export function PrivateMessageNotice({ communication, agentName }: {
  communication: TimelineCommunication; agentName: string;
}) {
  const [opened, setOpened] = useState(false);
  const { data: agents = [] } = useGetAgentsQuery();
  const identityColor = useIdentityColor();
  const { sessionId } = useParams();
  const { messages } = communication;
  const message = messages[messages.length - 1];
  const peer = message.external === undefined ? agents.find((agent) => agent.name === message.peer) : undefined;
  const identity = <><BotAvatar name={message.peer} sessionId={peer?.session_id} /><span>{message.peer}</span></>;
  const direction = message.direction === "sent" ? "已发消息给" : "消息来自";
  const DirectionIcon = message.direction === "sent" ? IconArrowUpRight : IconArrowDownLeft;
  const time = formatMessageTime(message.occurredAt);
  const title = `${direction} ${message.peer} · ${time.full}`;
  const directionIcon = <span className="private-message-direction" role="img" aria-label={direction}><DirectionIcon aria-hidden="true" size={17} /></span>;
  if (message.external === undefined) return <>
    <UnstyledButton className="private-message-trigger" aria-label={`与 ${message.peer} 的私聊`}
      aria-haspopup="dialog" onClick={() => setOpened(true)}>
      <IconMessages aria-hidden="true" size={15} />
      <span>与 {message.peer} 的私聊</span>
      <IconExternalLink aria-hidden="true" size={13} />
    </UnstyledButton>
    <Modal opened={opened} onClose={() => setOpened(false)} size={680} padding={0}
      className="private-message-modal" closeButtonProps={{ "aria-label": "关闭私聊" }}
      title={<span className="private-message-modal-title">
        <BotAvatar name={message.peer} sessionId={peer?.session_id} />
        <span>与 {message.peer} 的私聊</span>
      </span>}>
      <div className="private-message-transcript">
        {messages.map((item) => {
          const author = item.direction === "sent" ? agentName : item.peer;
          const recipient = item.direction === "sent" ? item.peer : agentName;
          const itemTime = formatMessageTime(item.occurredAt);
          return <article className="private-message-entry" key={item.key} data-direction={item.direction}
            aria-label={`${author} 发给 ${recipient} 的私信`}>
            <header className="private-message-entry-meta">
              <BotAvatar name={author} sessionId={item.direction === "sent" ? sessionId : peer?.session_id} />
              <span data-identity={identityColor(author)}>{author}</span>
              <time dateTime={item.occurredAt} title={itemTime.full}>{itemTime.short}</time>
            </header>
            <div className="private-message-entry-body"><MarkdownMessage content={item.text} streaming={false} /></div>
          </article>;
        })}
      </div>
      <div className="private-message-modal-footer">
        <span>{messages.length} 条消息</span>
        {peer === undefined ? null : <Link className="private-message-open-peer" to={`/sessions/${encodeURIComponent(peer.session_id)}`}>
          打开 {message.peer} 的对话 <IconExternalLink aria-hidden="true" size={14} />
        </Link>}
      </div>
    </Modal>
  </>;
  const notice = <>
    <span>外部协作者</span>
    {directionIcon}
    <Link className="private-message-peer" data-identity={identityColor(message.peer)}
      to={`/external/${encodeURIComponent(message.external.identity)}${sessionId === undefined ? "" : `?bot=${encodeURIComponent(sessionId)}`}`}>{identity}</Link>
  </>;
  return <div className="external-message">
    <div className="private-message-notice" title={title}>{notice}</div>
    <MarkdownMessage content={message.text} streaming={false} />
  </div>;
}
