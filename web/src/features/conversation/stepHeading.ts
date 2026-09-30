import type { VisibleStep, VisibleTool } from "./visibleTimeline";

const INTENT_LIMIT = 60;
const HINT_LIMIT = 48;
const LIST_MARKER = /^\s*(?:#{1,6}\s|[-*+>]\s|\d+[.)]\s)/;
const THINKING_HEADING = "思考中";

type HeadingStep = Pick<VisibleStep, "text" | "tools" | "thinkingPending">;

/**
 * 折叠行标题回答的是“这一步在做什么”，而不是“这一步调了哪些工具”。
 * 能说清楚就用模型自己的意图正文（首个非空行，截断到一行）；说不了就退回调用预览——
 * 工具名加第一个字符串参数，和展开态里工具的紧凑写法一致；仍在输出思考、尚无正文与工具时
 * 说“思考中”。标题只放一行，完整正文留给展开态。
 */
export function stepHeading(step: HeadingStep): string {
  const intent = stepIntent(step);
  if (intent !== null) {
    return intent;
  }
  if (step.tools.length > 0) {
    return step.tools.map(toolHeading).join(" · ");
  }
  return step.thinkingPending ? THINKING_HEADING : "Step";
}

function stepIntent(step: Pick<VisibleStep, "text">): string | null {
  for (const raw of (step.text ?? "").split("\n")) {
    const line = raw.replace(LIST_MARKER, "").trim();
    if (line !== "") {
      return clip(line, INTENT_LIMIT);
    }
  }
  return null;
}

function toolHeading(tool: VisibleTool): string {
  if (tool.name === "update_plan") {
    return tool.arguments.plan === null ? "结束当前计划" : "更新计划";
  }
  const hint = firstStringArgument(tool.arguments);
  return hint === null ? tool.name : `${tool.name}  ${hint}`;
}

function firstStringArgument(
  arguments_: Record<string, unknown>,
): string | null {
  for (const value of Object.values(arguments_)) {
    if (typeof value !== "string") {
      continue;
    }
    const text = value.trim();
    if (text) {
      return clip(text, HINT_LIMIT);
    }
  }
  return null;
}

function clip(text: string, limit: number): string {
  if (text.length <= limit) {
    return text;
  }
  return `${text.slice(0, limit - 1)}…`;
}
