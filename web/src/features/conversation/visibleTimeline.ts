import type { ConversationView, ToolStatus, VerificationFeedback } from "../../api/contracts";
import type {
  ActivePreview,
  LiveTool,
  SessionActivity,
} from "../../realtime/runtimeSlice";

export type VisibleUser = {
  key: string;
  kind: "user";
  occurredAt: string;
  text: string;
  images: string[];
  files: { attachment_id: string; name: string; size: number }[];
};

export type VisibleTool = {
  commandId: string;
  name: string;
  status: ToolStatus;
  error: string | null;
  arguments: Record<string, unknown>;
};

export type VisibleStep = {
  key: string;
  kind: "step";
  outputId: string;
  occurredAt: string | null;
  // 还在跑的那一步还没有 step_id。
  stepId: string | null;
  text: string | null;
  thinking: string | null;
  thinkingPending: boolean;
  pending: boolean;
  tools: VisibleTool[];
  files: { attachment_id: string; name: string; size: number }[];
};

export type VisiblePrivateMessage = {
  key: string;
  kind: "private_message";
  direction: "sent" | "received";
  peer: string;
  occurredAt: string;
  external?: { identity: string; text: string; revision: string | null; reference: string | null; previousRequestId: string | null; feedback: VerificationFeedback | null };
};

export type VisibleItem = VisibleUser | VisibleStep | VisiblePrivateMessage;

export function visibleTimeline(
  conversation: ConversationView,
  committed: Record<string, string>,
  preview: ActivePreview | null,
  tools: Record<string, LiveTool>,
  committedThinking: Record<string, string> = {},
  liveThinking: ActivePreview | null = null,
  activity: SessionActivity | null = null,
): VisibleItem[] {
  const journalOutputIds = new Set(
    conversation.items
      .filter((item) => item.kind === "step")
      .map((item) => item.output_id),
  );
  const items: VisibleItem[] = conversation.items.map((item) => {
    if (item.kind === "external_message") {
      return {
        key: `external:${item.message_id}`,
        kind: "private_message",
        direction: item.direction,
        peer: item.peer,
        occurredAt: item.occurred_at,
        external: { identity: item.identity, text: item.text, revision: item.revision, reference: item.reference, previousRequestId: item.previous_request_id, feedback: item.feedback },
      };
    }
    if (item.kind === "private_message") {
      return {
        key: `private:${item.direction}:${item.message_id}`,
        kind: "private_message",
        direction: item.direction,
        peer: item.peer,
        occurredAt: item.occurred_at,
      };
    }
    if (item.kind === "user") {
      return {
        key: item.message_id,
        kind: "user",
        occurredAt: item.occurred_at,
        text: item.text,
        images: item.images,
        files: item.files,
      };
    }
    return {
      key: `output:${item.output_id}`,
      kind: "step",
      outputId: item.output_id,
      occurredAt: item.occurred_at,
      stepId: item.step_id,
      text: item.text,
      pending: false,
      files: item.files ?? [],
      tools: item.tools.map((tool) => {
        const live = tools[tool.command_id];
        const status =
          live !== undefined &&
          (tool.status === "queued" || tool.status === "unknown")
            ? live.status
            : tool.status;
        return {
          commandId: tool.command_id,
          name: tool.name,
          status,
          error: status === "failed" || status === "unknown" ? tool.error : null,
          arguments: tool.arguments,
        };
      }),
      ...resolveThinking(item.output_id, item.thinking, committedThinking, liveThinking),
    };
  });
  for (const [outputId, text] of Object.entries(committed)) {
    if (journalOutputIds.has(outputId)) {
      continue;
    }
    if (activity !== "running") {
      continue;
    }
    items.push({
      key: `output:${outputId}`,
      kind: "step",
      outputId,
      occurredAt: null,
      stepId: null,
      text,
      pending: true,
      tools: [],
      files: [],
      ...resolveThinking(outputId, null, committedThinking, liveThinking),
    });
  }
  if (
    preview !== null &&
    !journalOutputIds.has(preview.outputId) &&
    committed[preview.outputId] === undefined
  ) {
    items.push({
      key: `output:${preview.outputId}`,
      kind: "step",
      outputId: preview.outputId,
      occurredAt: null,
      stepId: null,
      text: preview.text,
      pending: true,
      tools: [],
      files: [],
      ...resolveThinking(preview.outputId, null, committedThinking, liveThinking),
    });
  }
  const present = new Set(
    items.filter((item) => item.kind === "step").map((item) => item.outputId),
  );
  for (const outputId of thinkingOutputIds(committedThinking, liveThinking)) {
    if (present.has(outputId) || journalOutputIds.has(outputId)) {
      continue;
    }
    items.push({
      key: `output:${outputId}`,
      kind: "step",
      outputId,
      occurredAt: null,
      stepId: null,
      text: null,
      pending: true,
      tools: [],
      files: [],
      ...resolveThinking(outputId, null, committedThinking, liveThinking),
    });
  }
  return items;
}

function resolveThinking(
  outputId: string,
  journal: string | null,
  committedThinking: Record<string, string>,
  liveThinking: ActivePreview | null,
): { thinking: string | null; thinkingPending: boolean } {
  const live =
    liveThinking?.outputId === outputId ? liveThinking.text : undefined;
  const text = (live || committedThinking[outputId] || journal || "").trim();
  return {
    thinking: text || null,
    thinkingPending: live !== undefined,
  };
}

function thinkingOutputIds(
  committedThinking: Record<string, string>,
  liveThinking: ActivePreview | null,
): string[] {
  const ids = Object.keys(committedThinking);
  if (liveThinking !== null) {
    ids.push(liveThinking.outputId);
  }
  return ids;
}
