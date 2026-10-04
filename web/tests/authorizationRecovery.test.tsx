import { MantineProvider } from "@mantine/core";
import { configureStore } from "@reduxjs/toolkit";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Provider } from "react-redux";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { redpandaApi } from "../src/api/redpandaApi";
import type { ConversationView } from "../src/api/contracts";
import { Conversation } from "../src/features/conversation/Conversation";
import runtimeReducer, { connected, disconnected } from "../src/realtime/runtimeSlice";

// Rendering Markdown and editing drafts are outside the authorization contract.
vi.mock("../src/features/conversation/MarkdownMessage", () => ({
  MarkdownMessage: ({ content }: { content: string }) => <span>{content}</span>,
}));
vi.mock("../src/features/conversation/Composer", () => ({ Composer: () => null }));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("submits a recovered pending command with the new connection without an authorization SSE event", async () => {
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
  const commandId = "command-write";
  const view: ConversationView = {
    session_id: "session", workspace_id: "workspace", revision: 4,
    compact_count: 0, compact_phase: null, waiting_until: null, workspace_version: null, work_plan: null, work_plan_updates: [], context_input_tokens: null,
    session: {
      status: "waiting", waiting_for: [`authorization:${commandId}`],
      pending_authorization_ids: [commandId],
      pending_authorization_commands: [{ command_id: commandId, name: "write_file", arguments: { path: "probe.md" } }],
      should_wake: false, has_active_subagents: false, control_approval: null,
      control_message: null, auto_authorize: false, paused: false,
    },
    items: [
      { kind: "user", message_id: "user", text: "write probe", occurred_at: "2026-09-22T00:00:00Z", images: [], files: [] },
      { kind: "step", step_id: "step", output_id: "user", text: null, thinking: null,
        occurred_at: "2026-09-22T00:00:01Z", rewindable: false, tools: [{ command_id: commandId,
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
    reducer: { runtime: runtimeReducer, [redpandaApi.reducerPath]: redpandaApi.reducer },
    middleware: (getDefaultMiddleware) => getDefaultMiddleware().concat(redpandaApi.middleware),
  });
  store.dispatch(connected("old-connection"));
  render(
    <Provider store={store}><MantineProvider>
      <MemoryRouter initialEntries={["/sessions/session"]}>
        <Routes><Route path="/sessions/:sessionId" element={<Conversation />} /></Routes>
      </MemoryRouter>
    </MantineProvider></Provider>,
  );
  await screen.findByRole("button", { name: "拒绝" });
  await waitFor(() => expect(store.getState().runtime.ownerSessionId).toBe("session"));
  act(() => { store.dispatch(disconnected()); store.dispatch(connected("new-connection")); });
  await waitFor(() => expect(store.getState().runtime.ownerSessionId).toBe("session"));
  expect(store.getState().runtime.sessions.session.authorizations).toEqual({});
  fireEvent.click(screen.getByRole("button", { name: "拒绝" }));
  await waitFor(() => expect(submissions).toEqual([{ connection_id: "new-connection", approved: false }]));
  await waitFor(() => expect(screen.queryByRole("button", { name: "拒绝" })).toBeNull());
  cleanup();
  store.dispatch(redpandaApi.util.resetApiState());
});

it("disables pending authorization buttons while the web connection is down", async () => {
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
  const commandId = "command-write";
  const view: ConversationView = {
    session_id: "session", workspace_id: "workspace", revision: 4,
    compact_count: 0, compact_phase: null, waiting_until: null, workspace_version: null, work_plan: null, work_plan_updates: [], context_input_tokens: null,
    session: {
      status: "waiting", waiting_for: [`authorization:${commandId}`],
      pending_authorization_ids: [commandId],
      pending_authorization_commands: [{ command_id: commandId, name: "write_file", arguments: { path: "probe.md" } }],
      should_wake: false, has_active_subagents: false, control_approval: null,
      control_message: null, auto_authorize: false, paused: false,
    },
    items: [
      { kind: "user", message_id: "user", text: "write probe", occurred_at: "2026-09-22T00:00:00Z", images: [], files: [] },
      { kind: "step", step_id: "step", output_id: "user", text: null, thinking: null,
        occurred_at: "2026-09-22T00:00:01Z", rewindable: false, tools: [{ command_id: commandId,
          name: "write_file", status: "awaiting_authorization", error: null, arguments: { path: "probe.md" } }] },
    ],
  };
  const submissions: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => {
    const path = new URL(request.url).pathname;
    if (path.endsWith("/authorize")) {
      submissions.push(await request.json());
    }
    return Response.json(view);
  }));
  const store = configureStore({
    reducer: { runtime: runtimeReducer, [redpandaApi.reducerPath]: redpandaApi.reducer },
    middleware: (getDefaultMiddleware) => getDefaultMiddleware().concat(redpandaApi.middleware),
  });
  store.dispatch(connected("connection"));
  render(
    <Provider store={store}><MantineProvider>
      <MemoryRouter initialEntries={["/sessions/session"]}>
        <Routes><Route path="/sessions/:sessionId" element={<Conversation />} /></Routes>
      </MemoryRouter>
    </MantineProvider></Provider>,
  );
  const reject = await screen.findByRole("button", { name: "拒绝" });
  expect(reject).toBeEnabled();
  act(() => { store.dispatch(disconnected()); });
  expect(screen.getByRole("button", { name: "拒绝" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "允许" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "拒绝" }));
  expect(submissions).toEqual([]);
  expect(view.session.pending_authorization_ids).toEqual([commandId]);
  cleanup();
  store.dispatch(redpandaApi.util.resetApiState());
});
