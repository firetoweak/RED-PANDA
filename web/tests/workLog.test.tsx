import { MantineProvider } from "@mantine/core";
import { configureStore } from "@reduxjs/toolkit";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ComponentProps } from "react";
import { Provider } from "react-redux";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import type { ConversationView } from "../src/api/contracts";
import { redpandaApi } from "../src/api/redpandaApi";
import { Conversation } from "../src/features/conversation/Conversation";
import type { ConversationTimeline } from "../src/features/conversation/ConversationTimeline";
import runtimeReducer, {
  connected, outputFinal, previewDelta, previewStarted, sessionActivity, viewing,
} from "../src/realtime/runtimeSlice";

// Markdown rendering, the editor and virtualization are outside the work log contract.
vi.mock("../src/features/conversation/MarkdownMessage", () => ({
  MarkdownMessage: ({ content }: { content: string }) => <span>{content}</span>,
}));
vi.mock("../src/features/conversation/Composer", () => ({ Composer: () => null }));
vi.mock("../src/features/conversation/ConversationTimeline", () => ({
  ConversationTimeline: ({ turns, renderTurn }: ComponentProps<typeof ConversationTimeline>) => (
    <>{turns.map((turn) => <div key={turn.key}>{renderTurn(turn)}</div>)}</>
  ),
}));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("工作记录收成一行，展开才见全文；实时到来的工作记录同样不当作回复，也不算未读", async () => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  const NativeRequest = Request;
  vi.stubGlobal("Request", class extends NativeRequest {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      super(typeof input === "string" ? new URL(input, "http://localhost") : input, init);
    }
  });
  const view: ConversationView = {
    session_id: "agent", workspace_id: "workspace", revision: 6,
    compact_count: 0, compact_phase: null, waiting_until: null, work_plan: null, work_plan_updates: [], context_input_tokens: null,
    session: {
      status: "waiting", waiting_for: ["external_fact"], pending_authorization_ids: [], pending_authorization_commands: [],
      should_wake: false, has_active_subagents: false, control_approval: null,
      control_message: null, auto_authorize: false, paused: false, last_error: null,
    },
    items: [
      { kind: "user", message_id: "hi", text: "你好", occurred_at: "2026-10-19T00:00:00Z", images: [], files: [] },
      { kind: "step", audience: "user", step_id: "s1", output_id: "hi", text: "回给人", thinking: null,
        occurred_at: "2026-10-19T00:00:01Z", tools: [] },
      { kind: "step", audience: "log", step_id: "s2", output_id: "colleague", text: "先看看 Lin 说的文件", thinking: null,
        occurred_at: "2026-10-19T00:01:00Z", tools: [{ command_id: "read", name: "read_file", status: "succeeded",
          error: null, arguments: { path: "a.md" } }] },
      { kind: "step", audience: "log", step_id: "s3", output_id: "read", text: "已回复 Lin\n\n细节：文件没问题", thinking: null,
        occurred_at: "2026-10-19T00:01:05Z", tools: [] },
      { kind: "step", audience: "user", step_id: "s3:tell", output_id: "tell", text: "结论给你", thinking: null,
        occurred_at: "2026-10-19T00:01:05Z", tools: [] },
    ],
  };
  vi.stubGlobal("fetch", vi.fn(async (request: Request) =>
    Response.json(new URL(request.url).pathname.endsWith("/routines") ? [] : view)));
  const store = configureStore({
    reducer: { runtime: runtimeReducer, [redpandaApi.reducerPath]: redpandaApi.reducer },
    middleware: (getDefaultMiddleware) => getDefaultMiddleware().concat(redpandaApi.middleware),
  });
  store.dispatch(connected("connection"));
  render(
    <Provider store={store}><MantineProvider env="test">
      <MemoryRouter initialEntries={["/sessions/agent"]}>
        <Routes><Route path="/sessions/:sessionId" element={<Conversation />} /></Routes>
      </MemoryRouter>
    </MantineProvider></Provider>,
  );

  expect(await screen.findByText("回给人")).toBeInTheDocument();
  expect(screen.getByText("结论给你")).toBeInTheDocument();
  const entry = screen.getByRole("button", { name: /工作记录.*已回复 Lin/ });
  expect(entry).toHaveAttribute("aria-expanded", "false");
  expect(screen.queryByText(/细节：文件没问题/)).toBeNull();
  fireEvent.click(entry);
  expect(screen.getByText(/细节：文件没问题/)).toBeInTheDocument();

  // Elsewhere, a routine wakes the Agent: its live output is a work log too.
  act(() => {
    store.dispatch(viewing(null));
    store.dispatch(sessionActivity({ sessionId: "agent", activity: "running" }));
    store.dispatch(previewStarted({ sessionId: "agent", outputId: "routine", audience: "log" }));
    store.dispatch(previewDelta({ sessionId: "agent", outputId: "routine", text: "例行检查中\n\n正文还在写" }));
  });
  expect(screen.getByRole("button", { name: /工作记录.*例行检查中/ })).toHaveAttribute("aria-expanded", "false");
  expect(screen.queryByText(/正文还在写/)).toBeNull();
  act(() => {
    store.dispatch(outputFinal({ sessionId: "agent", outputId: "routine", text: "例行检查中\n\n正文写完了", audience: "log" }));
  });
  expect(store.getState().runtime.sessions.agent.unread).toBe(0);
  expect(screen.queryByText(/正文写完了/)).toBeNull();
  cleanup();
  store.dispatch(redpandaApi.util.resetApiState());
});
