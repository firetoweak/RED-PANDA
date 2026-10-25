import { MantineProvider } from "@mantine/core";
import { configureStore } from "@reduxjs/toolkit";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Provider } from "react-redux";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { kondenApi } from "../src/api/kondenApi";
import type { ConversationView } from "../src/api/contracts";
import { Conversation } from "../src/features/conversation/Conversation";
import runtimeReducer, { connected, disconnected } from "../src/realtime/runtimeSlice";
import type { ComponentProps } from "react";
import type { ConversationTimeline } from "../src/features/conversation/ConversationTimeline";

// Rendering Markdown and editing drafts are outside the authorization contract.
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

function prepareBrowser() {
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  vi.stubGlobal("matchMedia", () => ({
    matches: false, addEventListener() {}, removeEventListener() {},
  }));
  const NativeRequest = Request;
  vi.stubGlobal("Request", class extends NativeRequest {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      super(typeof input === "string" ? new URL(input, "http://localhost") : input, init);
    }
  });
}

it("submits a recovered pending command with the new connection without an authorization SSE event", async () => {
  prepareBrowser();
  const commandId = "command-write";
  const view: ConversationView = {
    session_id: "session", workspace_id: "workspace", revision: 4,
    compact_count: 0, compact_phase: null, waiting_until: null, work_plan: null, work_plan_updates: [], context_input_tokens: null,
    deleted: false, agent_name: null,
    session: {
      status: "waiting", waiting_for: [`authorization:${commandId}`],
      pending_authorization_ids: [commandId],
      pending_authorization_commands: [{ command_id: commandId, name: "write_file", arguments: { path: "probe.md" } }],
      should_wake: false, has_open_subagents: false, control_approval: null,
      control_message: null, auto_authorize: false, last_error: null,
    },
    items: [
      { kind: "user", message_id: "user", text: "write probe", occurred_at: "2026-09-22T00:00:00Z", images: [], files: [] },
      { kind: "step", step_id: "step", output_id: "user", text: null, thinking: null,
        occurred_at: "2026-09-22T00:00:01Z", tools: [{ command_id: commandId,
          name: "write_file", status: "awaiting_authorization", error: null, arguments: { path: "probe.md" } }] },
    ],
  };
  const submissions: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => {
    const path = new URL(request.url).pathname;
    if (path.endsWith("/authorize")) {
      submissions.push(await request.json());
      view.revision += 1;
      view.session.pending_authorization_ids = [];
      view.session.pending_authorization_commands = [];
      view.session.waiting_for = ["external_fact"];
      const step = view.items[1];
      if (step.kind === "step") step.tools[0].status = "rejected";
    }
    return Response.json(view);
  }));
  const store = configureStore({
    reducer: { runtime: runtimeReducer, [kondenApi.reducerPath]: kondenApi.reducer },
    middleware: (getDefaultMiddleware) => getDefaultMiddleware().concat(kondenApi.middleware),
  });
  store.dispatch(connected("old-connection"));
  render(
    <Provider store={store}><MantineProvider>
      <MemoryRouter initialEntries={["/sessions/session"]}>
        <Routes><Route path="/sessions/:sessionId" element={<Conversation />} /></Routes>
      </MemoryRouter>
    </MantineProvider></Provider>,
  );
  const process = await screen.findByRole("button", { name: /执行过程/ });
  expect(process).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(process);
  fireEvent.click(await screen.findByRole("button", { name: /1.*write_file/ }));
  await screen.findByRole("button", { name: "拒绝" });
  await waitFor(() => expect(store.getState().runtime.ownerSessionId).toBe("session"));
  act(() => { store.dispatch(disconnected()); });
  expect(screen.getByRole("button", { name: "拒绝" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "允许" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "拒绝" }));
  expect(submissions).toEqual([]);
  expect(view.session.pending_authorization_ids).toEqual([commandId]);
  act(() => { store.dispatch(connected("new-connection")); });
  await waitFor(() => expect(store.getState().runtime.ownerSessionId).toBe("session"));
  fireEvent.click(screen.getByRole("button", { name: "拒绝" }));
  await waitFor(() => expect(submissions).toEqual([{ connection_id: "new-connection", approved: false }]));
  await waitFor(() => expect(screen.queryByRole("button", { name: "拒绝" })).toBeNull());
  cleanup();
  store.dispatch(kondenApi.util.resetApiState());
});

it("recovers a model failure from the conversation after refresh and retries without submitting a message", async () => {
  prepareBrowser();
  const view: ConversationView = {
    session_id: "session", workspace_id: "workspace", revision: 1,
    compact_count: 0, compact_phase: null, waiting_until: null, work_plan: null, work_plan_updates: [], context_input_tokens: null,
    deleted: false, agent_name: null,
    session: {
      status: "runnable", waiting_for: [], pending_authorization_ids: [], pending_authorization_commands: [],
      should_wake: true, has_open_subagents: false, control_approval: null, control_message: null,
      auto_authorize: false, last_error: "供应商暂时不可用，请稍后再试。",
    },
    items: [{ kind: "user", message_id: "user", text: "原始请求", occurred_at: "2026-10-12T00:00:00Z", images: [], files: [] }],
  };
  const requests: { path: string; method: string; body: unknown }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => {
    const path = new URL(request.url).pathname;
    const body = request.method === "POST" ? await request.json() : null;
    requests.push({ path, method: request.method, body });
    if (path.endsWith("/retry")) {
      view.session.last_error = null;
      view.session.status = "waiting";
      view.session.waiting_for = ["external_fact"];
      view.session.should_wake = false;
    }
    return Response.json(view);
  }));
  const store = configureStore({
    reducer: { runtime: runtimeReducer, [kondenApi.reducerPath]: kondenApi.reducer },
    middleware: (getDefaultMiddleware) => getDefaultMiddleware().concat(kondenApi.middleware),
  });
  store.dispatch(connected("refreshed-connection"));
  render(<Provider store={store}><MantineProvider>
    <MemoryRouter initialEntries={["/sessions/session"]}>
      <Routes><Route path="/sessions/:sessionId" element={<Conversation />} /></Routes>
    </MemoryRouter>
  </MantineProvider></Provider>);
  expect(await screen.findByText(/执行已停止。供应商暂时不可用/)).toBeInTheDocument();
  await waitFor(() => expect(store.getState().runtime.ownerSessionId).toBe("session"));
  // 刷新后的状态只来自 GET / conversation，没有 session_failed 事件。
  expect(store.getState().runtime.sessions.session.lastError).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "查看详情" }));
  expect(screen.getByRole("region", { name: "执行已停止 · 错误详情" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "关闭内容页" }));
  expect(screen.queryByRole("region", { name: "执行已停止 · 错误详情" })).not.toBeInTheDocument();
  expect(store.getState().runtime.ownerSessionId).toBe("session");
  fireEvent.click(screen.getByRole("button", { name: "再试" }));
  await waitFor(() => expect(requests.filter((request) => request.path.endsWith("/retry"))).toEqual([
    { path: "/api/sessions/session/retry", method: "POST", body: { connection_id: "refreshed-connection" } },
  ]));
  await waitFor(() => expect(screen.queryByText(/执行已停止。供应商暂时不可用/)).toBeNull());
  expect(requests.filter((request) => request.method === "POST"
    && !request.path.endsWith("/select") && !request.path.endsWith("/retry"))).toEqual([]);
  expect(store.getState()[kondenApi.reducerPath].queries["getConversation(\"session\")"]?.data).toMatchObject({
    items: [{ kind: "user", message_id: "user", text: "原始请求" }],
  });
  cleanup();
  store.dispatch(kondenApi.util.resetApiState());
});
