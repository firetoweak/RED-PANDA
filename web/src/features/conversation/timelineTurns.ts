import type { VisibleItem, VisibleStep, VisibleUser } from "./visibleTimeline";

export type TimelineTurn = {
  key: string;
  user: VisibleUser | null;
  process: VisibleStep[];
  active: VisibleStep | null;
  final: VisibleStep | null;
};

export function timelineTurns(items: VisibleItem[]): TimelineTurn[] {
  const turns: Array<{ key: string; user: VisibleUser | null; steps: VisibleStep[] }> = [];
  let current: (typeof turns)[number] | null = null;

  for (const item of items) {
    if (item.kind === "user") {
      current = { key: item.key, user: item, steps: [] };
      turns.push(current);
      continue;
    }
    if (current === null) {
      current = { key: item.key, user: null, steps: [] };
      turns.push(current);
    }
    current.steps.push(item);
  }

  return turns.map(({ key, user, steps }) => {
    const candidate = steps.at(-1);
    const active = candidate?.pending === true ? candidate : null;
    const final = candidate !== undefined && isFinal(candidate) ? candidate : null;
    return {
      key,
      user,
      process: active === null && final === null ? steps : steps.slice(0, -1),
      active,
      final,
    };
  });
}

function isFinal(step: VisibleStep): boolean {
  return !step.pending && step.text !== null && step.tools.length === 0;
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

export function turnReply(
  turn: TimelineTurn,
  settled: boolean,
): { key: string; text: string; streaming: boolean } | null {
  const step = turn.final ?? turn.active;
  if (step === null) {
    return null;
  }
  const text = (step.text ?? "").trim();
  if (text === "") {
    return null;
  }
  return {
    key: step.key,
    text: step.text ?? "",
    streaming: !settled && turn.active !== null,
  };
}

export function turnElapsedMs(turn: TimelineTurn, settled: boolean): number | null {
  if (!settled || turn.user === null || turn.final === null || turn.final.occurredAt === null) {
    return null;
  }
  return Date.parse(turn.final.occurredAt) - Date.parse(turn.user.occurredAt);
}

export function formatElapsedTime(milliseconds: number): string {
  if (milliseconds < 60000) {
    return `${Math.round(milliseconds / 100) / 10} 秒`;
  }
  const totalSeconds = Math.round(milliseconds / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor(totalSeconds % 3600 / 60);
  const seconds = totalSeconds % 60;
  return hours > 0 ? `${hours} 小时 ${minutes} 分 ${seconds} 秒` : `${minutes} 分 ${seconds} 秒`;
}

export function turnNeedsSilentEnd(
  turn: TimelineTurn,
  settled: boolean,
): boolean {
  return settled && turn.process.length > 0 && turnReply(turn, settled) === null;
}

export function turnCanStartSession(
  turn: TimelineTurn,
  settled: boolean,
): boolean {
  return (
    settled &&
    turn.user !== null &&
    (turnReply(turn, settled) !== null || turnNeedsSilentEnd(turn, settled))
  );
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
