import { Link } from "react-router-dom";

import { useGetAgentsQuery } from "../../api/redpandaApi";
import { BotAvatar, useIdentityColor } from "./ChatPresentation";
import { formatMessageTime } from "./messageTime";
import type { VisiblePrivateMessage } from "./visibleTimeline";

export function PrivateMessageNotice({ message }: { message: VisiblePrivateMessage }) {
  const { data: agents = [] } = useGetAgentsQuery();
  const identityColor = useIdentityColor();
  const peer = agents.find((agent) => agent.name === message.peer);
  const identity = <><BotAvatar name={message.peer} sessionId={peer?.session_id} /><span>{message.peer}</span></>;
  return <div className="private-message-notice" title={formatMessageTime(message.occurredAt).full}>
    <span>{message.direction === "sent" ? "已发消息给" : "消息来自"}</span>
    {peer === undefined
      ? <span className="private-message-peer" data-identity={identityColor(message.peer)}>{identity}</span>
      : <Link className="private-message-peer" data-identity={identityColor(message.peer)}
        to={`/sessions/${encodeURIComponent(peer.session_id)}`}>{identity}</Link>}
  </div>;
}
