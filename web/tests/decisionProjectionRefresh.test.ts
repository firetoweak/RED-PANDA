import { afterEach, expect, it, vi } from "vitest";

import { redpandaApi } from "../src/api/redpandaApi";
import type { AppDispatch } from "../src/app/store";
import { openEventBridge } from "../src/realtime/eventBridge";

afterEach(() => vi.unstubAllGlobals());

it("refreshes committed projections when the next model call starts, before any output", () => {
  const listeners = new Map<string, (event: MessageEvent) => void>();
  vi.stubGlobal("EventSource", class {
    addEventListener(name: string, listener: (event: MessageEvent) => void) {
      listeners.set(name, listener);
    }
    close() {}
  });
  const dispatch = vi.fn();
  const close = openEventBridge(dispatch as AppDispatch);
  try {
    listeners.get("preview.started")!(new MessageEvent("preview.started", {
      data: JSON.stringify({ session_id: "s", output_id: "decision" }),
    }));
    expect(dispatch.mock.calls.map(([action]) => action)).toContainEqual(
      redpandaApi.util.invalidateTags([{ type: "Conversation", id: "s" }]),
    );
  } finally {
    close();
  }
});
