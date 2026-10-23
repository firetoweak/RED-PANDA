import { Link } from "react-router-dom";

import { useGetAgentsQuery } from "../../api/redpandaApi";
import { BotAvatar, useIdentityColor } from "./ChatPresentation";
import { formatMessageTime } from "./messageTime";
import type { VisiblePrivateMessage } from "./visibleTimeline";
import { MarkdownMessage } from "./MarkdownMessage";

export function PrivateMessageNotice({ message }: { message: VisiblePrivateMessage }) {
  const { data: agents = [] } = useGetAgentsQuery();
  const identityColor = useIdentityColor();
  const peer = message.external === undefined ? agents.find((agent) => agent.name === message.peer) : undefined;
  const identity = <><BotAvatar name={message.peer} sessionId={peer?.session_id} /><span>{message.peer}</span></>;
  const notice = <div className="private-message-notice" title={formatMessageTime(message.occurredAt).full}>
    {message.external === undefined ? null : <span>外部协作者</span>}
    <span>{message.direction === "sent" ? "已发消息给" : "消息来自"}</span>
    {peer === undefined
      ? <span className="private-message-peer" data-identity={identityColor(message.peer)}>{identity}</span>
      : <Link className="private-message-peer" data-identity={identityColor(message.peer)}
        to={`/sessions/${encodeURIComponent(peer.session_id)}`}>{identity}</Link>}
  </div>;
  if (message.external === undefined) return notice;
  const feedback = message.external.feedback;
  const label = feedback === null ? (message.external.previousRequestId === null ? "独立验证请求" : "复验 / 补充请求")
    : feedback.kind === "question" ? "验证提问" : feedback.kind === "progress" ? "验证进度"
    : `验证 Bot 报告：${feedback.verdict === "passed" ? "通过" : feedback.verdict === "issues_found" ? "发现问题" : "受阻"}`;
  return <div className="external-message">
    {notice}
    <div className="external-message-version">{label}</div>
    <div className="external-message-version">会话身份 <code>{message.external.identity}</code></div>
    {message.external.revision === null ? null : <div className="external-message-version">请求版本 <code>{message.external.revision}</code></div>}
    {message.external.reference === null ? null : <div className="external-message-version">引用：{message.external.reference}</div>}
    {feedback?.tested_revision == null ? null : <div className="external-message-version">实际验证版本 <code>{feedback.tested_revision}</code></div>}
    {feedback === null || feedback.evidence.length === 0 ? null : <div className="external-message-version">证据：{feedback.evidence.join("；")}</div>}
    <MarkdownMessage content={message.external.text} streaming={false} />
  </div>;
}
