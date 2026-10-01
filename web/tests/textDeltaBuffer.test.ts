import { expect, it, vi } from "vitest";

import { createTextDeltaBuffer, type TextDelta } from "../src/realtime/textDeltaBuffer";

it("batches interleaved sessions independently until the same frame", () => {
  const published: TextDelta[] = [];
  const scheduled: { flush: (() => void) | null } = { flush: null };
  const schedule = vi.fn((flush: () => void) => {
    scheduled.flush = flush;
    return () => { scheduled.flush = null; };
  });
  const buffer = createTextDeltaBuffer((delta) => published.push(delta), schedule);

  for (let index = 0; index < 100; index++) {
    buffer.enqueue({ sessionId: `s${index % 2}`, outputId: "out", text: "字" });
  }

  expect(published).toEqual([]);
  expect(schedule).toHaveBeenCalledTimes(1);
  scheduled.flush?.();
  expect(published).toEqual([
    { sessionId: "s0", outputId: "out", text: "字".repeat(50) },
    { sessionId: "s1", outputId: "out", text: "字".repeat(50) },
  ]);
});

it("flushing one session preserves the other session's scheduled batch", () => {
  const published: TextDelta[] = [];
  const scheduled: { flush: (() => void) | null } = { flush: null };
  const cancel = vi.fn();
  const buffer = createTextDeltaBuffer(
    (delta) => published.push(delta),
    (flush) => { scheduled.flush = flush; return cancel; },
  );
  buffer.enqueue({ sessionId: "s1", outputId: "out", text: "甲" });
  buffer.enqueue({ sessionId: "s2", outputId: "out", text: "乙" });

  buffer.flushNow("s1");
  buffer.enqueue({ sessionId: "s2", outputId: "out", text: "丙" });
  expect(published).toEqual([{ sessionId: "s1", outputId: "out", text: "甲" }]);
  expect(cancel).not.toHaveBeenCalled();

  scheduled.flush?.();
  expect(published).toEqual([
    { sessionId: "s1", outputId: "out", text: "甲" },
    { sessionId: "s2", outputId: "out", text: "乙丙" },
  ]);
});

it("forced flush cancels the frame and allows scheduling the next batch", () => {
  const publish = vi.fn();
  const cancel = vi.fn();
  const schedule = vi.fn(() => cancel);
  const buffer = createTextDeltaBuffer(publish, schedule);
  buffer.enqueue({ sessionId: "s1", outputId: "out", text: "甲" });
  buffer.enqueue({ sessionId: "s2", outputId: "out", text: "乙" });

  buffer.flushNow();
  expect(cancel).toHaveBeenCalledTimes(1);
  expect(publish).toHaveBeenCalledTimes(2);
  buffer.enqueue({ sessionId: "s1", outputId: "out", text: "丙" });
  expect(schedule).toHaveBeenCalledTimes(2);
});
