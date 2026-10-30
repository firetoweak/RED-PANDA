import type { VisibleItem, VisiblePrivateMessage, VisibleStep, VisibleUser } from "./visibleTimeline";

export type TimelineCommunication = {
  key: string;
  message: VisiblePrivateMessage;
  messages: VisiblePrivateMessage[];
};

export type TimelineTurn = {
  key: string;
  user: VisibleUser | null;
  communications: TimelineCommunication[];
  process: VisibleStep[];
  active: VisibleStep | null;
  final: VisibleStep | null;
};

export function timelineTurns(items: VisibleItem[]): TimelineTurn[] {
  const turns: Array<{ key: string; user: VisibleUser | null; steps: VisibleStep[]; communications: VisiblePrivateMessage[] }> = [];
  const privateThreads = new Map<string, VisiblePrivateMessage[]>();
  let current: (typeof turns)[number] | null = null;

  for (const item of items) {
    if (item.kind === "user") {
      current = { key: item.key, user: item, steps: [], communications: [] };
      turns.push(current);
      continue;
    }
    const previous = current?.steps.at(-1);
    if (current === null || (previous !== undefined && isFinal(previous))) {
      current = { key: item.key, user: null, steps: [], communications: [] };
      turns.push(current);
    }
    if (item.kind === "private_message") {
      current.communications.push(item);
      if (item.threadId !== null) {
        const thread = privateThreads.get(item.threadId) ?? [];
        thread.push(item);
        privateThreads.set(item.threadId, thread);
      }
      continue;
    }
    current.steps.push(item);
  }

  for (const [threadId, messages] of privateThreads) {
    privateThreads.set(threadId, orderPrivateMessages(messages));
  }
  return turns.map(({ key, user, steps, communications }) => {
    const candidate = steps.at(-1);
    const active = candidate?.pending === true ? candidate : null;
    const final = candidate !== undefined && isFinal(candidate) ? candidate : null;
    return {
      key,
      user,
      communications: communications.map((message) => ({
        key: message.key,
        message,
        messages: message.threadId === null ? [message] : privateThreads.get(message.threadId)!,
      })),
      process: active === null && final === null ? steps : steps.slice(0, -1),
      active,
      final,
    };
  });
}

function orderPrivateMessages(messages: VisiblePrivateMessage[]): VisiblePrivateMessage[] {
  const byId = new Map(messages.map((message) => [message.messageId, message]));
  const visited = new Set<string>();
  const ordered: VisiblePrivateMessage[] = [];
  function append(message: VisiblePrivateMessage) {
    if (visited.has(message.messageId)) return;
    // 对方的回复可能先于本地发送确认到达；被回复的消息始终在前。
    const parent = message.replyTo === null ? undefined : byId.get(message.replyTo);
    if (parent !== undefined) append(parent);
    visited.add(message.messageId);
    ordered.push(message);
  }
  for (const message of messages) append(message);
  return ordered;
}

function isFinal(step: VisibleStep): boolean {
  return !step.pending && (step.text !== null || step.files.length > 0) && step.tools.length === 0;
}

function stepHasLiveWork(step: VisibleStep): boolean {
  return (
    step.pending ||
    step.tools.some(
      (tool) =>
        tool.status === "queued" ||
        tool.status === "running" ||
        tool.status === "awaiting_authorization",
    )
  );
}

function processHasLiveWork(turn: TimelineTurn): boolean {
  return turn.process.some(stepHasLiveWork);
}

export function turnIsSettled(
  turn: TimelineTurn,
  options: {
    latest: boolean;
    running: boolean;
    awaitingControl: boolean;
  },
): boolean {
  if (processHasLiveWork(turn)) {
    return false;
  }
  if (!options.latest) {
    return true;
  }
  return !options.running && !options.awaitingControl;
}

export function turnReplies(
  turn: TimelineTurn,
  settled: boolean,
): { key: string; text: string; streaming: boolean; files: VisibleStep["files"] }[] {
  return [...turn.process, turn.active, turn.final]
    .filter((step): step is VisibleStep => step !== null
      && ((step.text ?? "").trim() !== "" || (step.files ?? []).length > 0))
    .map((step) => ({
      key: step.key,
      text: step.text ?? "",
      files: step.files ?? [],
      streaming: !settled && step.pending,
    }));
}

export function turnNeedsSilentEnd(
  turn: TimelineTurn,
  settled: boolean,
): boolean {
  return settled && turn.process.length > 0 && turn.communications.length === 0 && turnReplies(turn, settled).length === 0;
}

function turnHasThinking(turn: TimelineTurn): boolean {
  return [turn.active, turn.final, ...turn.process].some(
    (step) => step !== null && (step.thinking ?? "").trim() !== "",
  );
}

export function turnNeedsThinkingHint(
  turn: TimelineTurn,
  options: { running: boolean; latest: boolean; settled?: boolean },
): boolean {
  if (options.settled === true) {
    return false;
  }
  if (turnHasThinking(turn)) {
    return false;
  }
  if (turn.active !== null) {
    return !(turn.active.text ?? "").trim();
  }
  if (!options.running || !options.latest || turn.final !== null) {
    return false;
  }
  return !turn.process.some((step) =>
    step.tools.some(
      (tool) =>
        tool.status === "queued" ||
        tool.status === "running" ||
        tool.status === "awaiting_authorization",
    ),
  );
}
