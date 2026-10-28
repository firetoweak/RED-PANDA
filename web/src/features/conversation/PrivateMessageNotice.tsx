import { UnstyledButton } from "@mantine/core";
import { IconArrowDownLeft, IconArrowUpRight, IconChevronDown, IconExternalLink, IconMessages } from "@tabler/icons-react";
import { useId, useState } from "react";
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
  const contentId = useId();
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
  if (message.external === undefined) return <div className={`private-message-card${opened ? " is-open" : ""}`}>
    <div className="private-message-header">
      <UnstyledButton className="private-message-notice private-message-toggle" aria-label={`与 ${message.peer} 的交流`}
        aria-expanded={opened} aria-controls={contentId} title={`与 ${message.peer} 的交流 · ${messages.length} 条消息 · ${time.full}`}
        onClick={() => setOpened((value) => !value)}>
        <IconMessages aria-hidden="true" size={17} />
        <span>与</span>
        <span className="private-message-peer" data-identity={identityColor(message.peer)}>{identity}</span>
        <span>的交流</span>
        <span className="private-message-count">{messages.length} 条消息</span>
        <time dateTime={message.occurredAt}>{time.short}</time>
        <IconChevronDown className="private-message-chevron" aria-hidden="true" size={14} />
      </UnstyledButton>
      {peer === undefined ? null : <Link className="private-message-open-peer" to={`/sessions/${encodeURIComponent(peer.session_id)}`}
        aria-label={`打开 ${message.peer} 的对话`} title={`打开 ${message.peer} 的对话`}>
        <IconExternalLink aria-hidden="true" size={16} />
      </Link>}
    </div>
    <div id={contentId} className="private-message-content" hidden={!opened}>
      {opened ? messages.map((item) => {
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
          <MarkdownMessage content={item.text} streaming={false} />
        </article>;
      }) : null}
    </div>
  </div>;
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
