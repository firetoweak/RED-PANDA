import type { VisibleTool } from "./visibleTimeline";

export function isSubagentTool(name: string): boolean {
  return name === "delegate" || name === "reclaim";
}

/** 当前轮次、父仍有未回收的子时，画「子 Agent 执行中」。不看 delegate 命令终态。 */
export function turnNeedsSubagentHint(latest: boolean, active: boolean): boolean {
  return latest && active;
}

export function subagentTask(tool: VisibleTool): string | null {
  const value = tool.arguments.task;
  return typeof value === "string" && value.trim() !== "" ? value.trim() : null;
}

export function subagentChildSessionId(tool: VisibleTool): string | null {
  const value = tool.arguments.child_session_id;
  return typeof value === "string" && value !== "" ? value : null;
}
