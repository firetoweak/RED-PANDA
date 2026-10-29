import type { ExternalSessionSummary } from "../../api/contracts";

const labels = {
  disabled: "需主动收信", ready: "自动接续已启用", pending: "有消息待提醒",
  queued: "提醒已入队", failed: "提醒失败", unknown: "提醒结果未确认",
};

export function sessionStatus(session: ExternalSessionSummary) {
  return session.active ? labels[session.codex_status] : "接入已撤销";
}
