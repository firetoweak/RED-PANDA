export type TextDelta = {
  sessionId: string;
  outputId: string;
  text: string;
};

export function createTextDeltaBuffer(
  publish: (delta: TextDelta) => void,
  schedule: (flush: () => void) => () => void = scheduleAnimationFrame,
) {
  const pending = new Map<string, TextDelta>();
  let cancel: (() => void) | null = null;

  function flush() {
    cancel = null;
    const deltas = [...pending.values()];
    pending.clear();
    for (const delta of deltas) {
      publish(delta);
    }
  }

  function enqueue(delta: TextDelta) {
    const previous = pending.get(delta.sessionId);
    if (previous !== undefined && previous.outputId !== delta.outputId) {
      pending.set(delta.sessionId, delta);
      publish(previous);
    } else if (previous !== undefined) {
      pending.set(delta.sessionId, {
        sessionId: previous.sessionId,
        outputId: previous.outputId,
        text: previous.text + delta.text,
      });
    } else {
      pending.set(delta.sessionId, delta);
    }
    if (cancel === null) {
      cancel = schedule(flush);
    }
  }

  function flushNow(sessionId?: string) {
    if (sessionId === undefined) {
      cancel?.();
      flush();
      return;
    }
    const delta = pending.get(sessionId);
    pending.delete(sessionId);
    if (pending.size === 0) {
      cancel?.();
      cancel = null;
    }
    if (delta !== undefined) {
      publish(delta);
    }
  }

  return { enqueue, flushNow };
}

function scheduleAnimationFrame(flush: () => void): () => void {
  const id = requestAnimationFrame(flush);
  return () => cancelAnimationFrame(id);
}
