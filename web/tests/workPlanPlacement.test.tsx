import { MantineProvider } from "@mantine/core";
import { configureStore } from "@reduxjs/toolkit";
import { act, cleanup, render, screen } from "@testing-library/react";
import { Provider } from "react-redux";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import type { ConversationView, WorkPlan } from "../src/api/contracts";
import { redpandaApi } from "../src/api/redpandaApi";
import { Conversation } from "../src/features/conversation/Conversation";
import runtimeReducer from "../src/realtime/runtimeSlice";

vi.mock("../src/features/conversation/MarkdownMessage", () => ({
  MarkdownMessage: ({ content }: { content: string }) => <span>{content}</span>,
}));
vi.mock("../src/features/conversation/Composer", () => ({ Composer: () => null }));

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("已完成计划留在对应回复之后，新计划固定在输入区域且不覆盖旧计划", async () => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  const NativeRequest = Request;
  vi.stubGlobal("Request", class extends NativeRequest {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      super(typeof input === "string" ? new URL(input, "http://localhost") : input, init);
    }
  });
  const completed: WorkPlan = {
    objective: "第一项任务", steps: [{ text: "验证结果", status: "completed" }], note: null,
  };
  const active: WorkPlan = {
    objective: "第二项任务", steps: [{ text: "调查原因", status: "in_progress" }], note: null,
  };
  const view: ConversationView = {
    session_id: "session", workspace_id: "workspace", revision: 5,
    compact_count: 0, compact_phase: null, waiting_until: null, workspace_version: null,
    context_input_tokens: null, work_plan: completed,
    work_plan_updates: [{ step_id: "plan-step", plan: completed }],
    session: {
      status: "waiting", waiting_for: ["external_fact"], pending_authorization_ids: [],
      pending_authorization_commands: [], should_wake: false, has_active_subagents: false,
      control_approval: null, control_message: null, auto_authorize: false, paused: false,
    },
    items: [
      { kind: "user", message_id: "first", text: "第一轮请求", occurred_at: "2026-10-03T00:00:00Z", images: [], files: [] },
      { kind: "step", step_id: "plan-step", output_id: "plan-output", text: "更新计划", thinking: null,
        occurred_at: "2026-10-03T00:00:01Z", rewindable: false,
        tools: [{ command_id: "plan-command", name: "update_plan", status: "succeeded", error: null, arguments: {} }] },
      { kind: "step", step_id: "reply-step", output_id: "reply-output", text: "第一轮完成回复", thinking: null,
        occurred_at: "2026-10-03T00:00:02Z", rewindable: false, tools: [] },
    ],
  };
  vi.stubGlobal("fetch", vi.fn(async () => Response.json(view)));
  const store = configureStore({
    reducer: { runtime: runtimeReducer, [redpandaApi.reducerPath]: redpandaApi.reducer },
    middleware: (getDefaultMiddleware) => getDefaultMiddleware().concat(redpandaApi.middleware),
  });
  render(<Provider store={store}><MantineProvider>
    <MemoryRouter initialEntries={["/sessions/session"]}>
      <Routes><Route path="/sessions/:sessionId" element={<Conversation />} /></Routes>
    </MemoryRouter>
  </MantineProvider></Provider>);
  const completedPanel = await screen.findByRole("button", { name: "已完成计划" });
  const firstTurn = screen.getByText("第一轮完成回复").closest(".message-assistant")!.parentElement!;
  expect(firstTurn).toContainElement(completedPanel);
  expect(document.querySelector(".composer-dock .work-plan")).toBeNull();
  expect(screen.getByText("第一轮完成回复").compareDocumentPosition(completedPanel) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

  const next: ConversationView = {
    ...view, work_plan: active,
    work_plan_updates: [...view.work_plan_updates, { step_id: "next-plan-step", plan: active }],
    items: [...view.items,
      { kind: "user", message_id: "second", text: "第二轮请求", occurred_at: "2026-10-03T00:01:00Z", images: [], files: [] },
      { kind: "step", step_id: "next-plan-step", output_id: "next-output", text: "调查中", thinking: null,
        occurred_at: "2026-10-03T00:01:01Z", rewindable: false, tools: [] },
    ],
  };
  await act(async () => { await store.dispatch(redpandaApi.util.upsertQueryData("getConversation", "session", next)); });
  const currentPanel = await screen.findByRole("button", { name: "当前计划" });
  expect(document.querySelector(".composer-dock")).toContainElement(currentPanel);
  expect(firstTurn).toContainElement(screen.getByRole("button", { name: "已完成计划" }));
  cleanup();
  store.dispatch(redpandaApi.util.resetApiState());
});
