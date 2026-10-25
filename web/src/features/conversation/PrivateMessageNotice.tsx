import { IconArrowDownLeft, IconArrowUpRight } from "@tabler/icons-react";
import { Link, useParams } from "react-router-dom";

import { useGetAgentsQuery } from "../../api/kondenApi";
import { BotAvatar, useIdentityColor } from "./ChatPresentation";
import { formatMessageTime } from "./messageTime";
import type { VisiblePrivateMessage } from "./visibleTimeline";
import { MarkdownMessage } from "./MarkdownMessage";

export function PrivateMessageNotice({ message }: { message: VisiblePrivateMessage }) {
  const { data: agents = [] } = useGetAgentsQuery();
  const identityColor = useIdentityColor();
  const { sessionId } = useParams();
  const peer = message.external === undefined ? agents.find((agent) => agent.name === message.peer) : undefined;
  const identity = <><BotAvatar name={message.peer} sessionId={peer?.session_id} /><span>{message.peer}</span></>;
  const direction = message.direction === "sent" ? "已发消息给" : "消息来自";
  const DirectionIcon = message.direction === "sent" ? IconArrowUpRight : IconArrowDownLeft;
  const notice = <div className="private-message-notice" title={`${direction} ${message.peer} · ${formatMessageTime(message.occurredAt).full}`}>
    {message.external === undefined ? null : <span>外部协作者</span>}
    <span className="private-message-direction" role="img" aria-label={direction}><DirectionIcon aria-hidden="true" size={17} /></span>
    {message.external !== undefined
      ? <Link className="private-message-peer" data-identity={identityColor(message.peer)}
          to={`/external/${encodeURIComponent(message.external.identity)}${sessionId === undefined ? "" : `?bot=${encodeURIComponent(sessionId)}`}`}>{identity}</Link>
      : peer === undefined ? <span className="private-message-peer" data-identity={identityColor(message.peer)}>{identity}</span>
      : <Link className="private-message-peer" data-identity={identityColor(message.peer)}
        to={`/sessions/${encodeURIComponent(peer.session_id)}`}>{identity}</Link>}
  </div>;
  if (message.external === undefined) return notice;
  return <div className="external-message">
    {notice}
    <MarkdownMessage content={message.external.text} streaming={false} />
  </div>;
}
