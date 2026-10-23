import { MantineProvider } from "@mantine/core";
import { configureStore } from "@reduxjs/toolkit";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ComponentProps } from "react";
import { Provider } from "react-redux";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { ConversationView, GroupView } from "../src/api/contracts";
import { redpandaApi } from "../src/api/redpandaApi";
import type { ConversationTimeline } from "../src/features/conversation/ConversationTimeline";
import { SessionConversation } from "../src/features/conversation/SessionConversation";
import { GroupChatPage } from "../src/features/groups/GroupChatPage";
import runtimeReducer, { connected, disconnected, viewing } from "../src/realtime/runtimeSlice";

// Markdown and virtual-list layout are independent of request ownership and order.
vi.mock("../src/features/conversation/MarkdownMessage", () => ({
  MarkdownMessage: ({ content }: { content: string }) => <span>{content}</span>,
}));
vi.mock("../src/features/conversation/Composer", () => ({ Composer: () => null }));
vi.mock("../src/features/conversation/ConversationTimeline", () => ({
  ConversationTimeline: ({ turns, renderTurn }: ComponentProps<typeof ConversationTimeline>) => (
    <>{turns.map((turn) => <div key={turn.key}>{renderTurn(turn)}</div>)}</>
  ),
}));

function snapshot(id: string, revision = 1): ConversationView {
  return {
    session_id: id, workspace_id: null, revision,
    compact_count: 0, compact_phase: null, waiting_until: null, work_plan: null,
    work_plan_updates: [], context_input_tokens: null,
    deleted: false, agent_name: null,
    session: {
      status: "waiting", waiting_for: ["external_fact"], pending_authorization_ids: [],
      pending_authorization_commands: [], should_wake: false, has_open_subagents: false,
      control_approval: null, control_message: null, auto_authorize: false, last_error: null,
    },
    items: [{
      kind: "user", message_id: `user-${id}`, text: `来自 ${id} 的消息`,
      occurred_at: "2026-10-23T00:00:00Z", images: [], files: [],
    }],
  };
}

function deferred() {
  let resolve!: (value: Response) => void;
  const promise = new Promise<Response>((done) => { resolve = done; });
  return { promise, resolve };
}

function createStore() {
  return configureStore({
    reducer: { runtime: runtimeReducer, [redpandaApi.reducerPath]: redpandaApi.reducer },
    middleware: (defaults) => defaults().concat(redpandaApi.middleware),
  });
}

const stores: ReturnType<typeof createStore>[] = [];
beforeEach(() => {
  const NativeRequest = Request;
  vi.stubGlobal("Request", class extends NativeRequest {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      super(typeof input === "string" ? new URL(input, "http://localhost") : input, init);
    }
  });
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  Object.defineProperty(document, "fonts", { configurable: true, value: { addEventListener() {}, removeEventListener() {} } });
});
afterEach(() => {
  cleanup();
  for (const store of stores.splice(0)) store.dispatch(redpandaApi.util.resetApiState());
  vi.unstubAllGlobals();
  Reflect.deleteProperty(document, "fonts");
  localStorage.clear();
});

it("删除后的私聊保留 Bot 名字与历史，不能再发送", async () => {
  const store = createStore();
  stores.push(store);
  const history = snapshot("removed");
  history.deleted = true;
  history.agent_name = "历史 Ada";
  history.items.push({ kind: "step", step_id: "done", output_id: "done", text: "保留的回复",
    thinking: null, tools: [], files: [], occurred_at: "2026-10-23T00:00:01Z" });
  vi.stubGlobal("fetch", vi.fn((request: Request) => {
    const path = new URL(request.url).pathname;
    return Promise.resolve(Response.json(path.endsWith("/agents") || path.endsWith("/groups")
      || path.endsWith("/workspaces") || path.endsWith("/commands") ? [] : history));
  }));
  render(<Provider store={store}><MantineProvider env="test"><MemoryRouter initialEntries={["/sessions/removed"]}>
    <Routes><Route path="/sessions/:sessionId" element={<SessionConversation />} /></Routes>
  </MemoryRouter></MantineProvider></Provider>);
  expect(await screen.findByText("历史 Ada", { exact: true })).toBeInTheDocument();
  expect(await screen.findByRole("article", { name: "历史 Ada 的回复" })).toHaveTextContent("保留的回复");
  expect(screen.getByText("Bot 已删除，此处仅保留聊天记录。")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "发送", exact: true })).not.toBeInTheDocument();
});

it.each(["query-last", "mutation-last"])("较新快照不被旧响应覆盖：%s", async (order) => {
  const store = createStore();
  stores.push(store);
  const get = deferred();
  const send = deferred();
  const fetch = vi.fn((request: Request) => request.method === "GET" ? get.promise : send.promise);
  vi.stubGlobal("fetch", fetch);
  const older = snapshot("Ada", 2);
  const newer = snapshot("Ada", 5);
  newer.items.push({
    kind: "step", step_id: "step-final", output_id: "output-final", text: "最终回复",
    thinking: null, tools: [], files: [], occurred_at: "2026-10-23T00:00:01Z",
  });
  const query = store.dispatch(redpandaApi.endpoints.getConversation.initiate("Ada"));
  const mutation = store.dispatch(redpandaApi.endpoints.sendInput.initiate({
    connectionId: "connection", sessionId: "Ada", deliveryId: "delivery", text: "你好", artifactRefs: [], interrupt: false,
  }));
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  if (order === "query-last") {
    send.resolve(Response.json(newer));
    await mutation.unwrap();
    await waitFor(() => expect(redpandaApi.endpoints.getConversation.select("Ada")(store.getState()).data?.revision).toBe(5));
    get.resolve(Response.json(older));
  } else {
    get.resolve(Response.json(newer));
    await query.unwrap();
    send.resolve(Response.json(older));
  }
  await Promise.all([query, mutation]);
  await waitFor(() => expect(redpandaApi.endpoints.getConversation.select("Ada")(store.getState()).data).toEqual(newer));
});

it("旧连接的选择确认晚到，不能重新绑定已经失效的连接", async () => {
  const store = createStore();
  stores.push(store);
  const response = deferred();
  const fetch = vi.fn(() => response.promise);
  vi.stubGlobal("fetch", fetch);
  store.dispatch(connected("old"));
  store.dispatch(viewing("Ada"));
  const request = store.dispatch(redpandaApi.endpoints.selectSession.initiate({ connectionId: "old", sessionId: "Ada" }));
  await waitFor(() => expect(fetch).toHaveBeenCalledOnce());
  store.dispatch(disconnected());
  store.dispatch(connected("new"));
  response.resolve(Response.json(snapshot("Ada")));
  await request.unwrap();
  expect(store.getState().runtime.connectionId).toBe("new");
  expect(store.getState().runtime.ownerSessionId).toBeNull();
});

it.each(["success", "failure"])("切换 Bot 和群之后，旧 Bot 请求%s不改变当前页面", async (result) => {
  const store = createStore();
  stores.push(store);
  const oldGet = deferred();
  const oldSelect = deferred();
  const selections: string[] = [];
  const group: GroupView = {
    group_id: "team", name: "测试群", members: ["Ada", "Lin"], dissolved: false, calling: [],
    messages: [{ message_id: "group-message", sender: "Lin", text: "群里的消息", mentioned_agents: [], reply_to: null, occurred_at: "2026-10-23T00:00:00Z" }],
  };
  vi.stubGlobal("fetch", vi.fn((request: Request) => {
    const path = new URL(request.url).pathname;
    if (path === "/api/sessions/Ada") return oldGet.promise;
    if (path.endsWith("/select")) {
      selections.push(path);
      return path.includes("/Ada/") ? oldSelect.promise : Promise.resolve(Response.json(snapshot("Lin")));
    }
    if (path === "/api/sessions/Lin") return Promise.resolve(Response.json(snapshot("Lin")));
    if (path === "/api/groups/team") return Promise.resolve(Response.json(group));
    if (path === "/api/agents") return Promise.resolve(Response.json(["Ada", "Lin"].map((id) => ({
      session_id: id, name: id, projects: [], activity: "idle", status: "idle",
    }))));
    if (["/api/groups", "/api/workspaces", "/api/inbox"].includes(path) || path.endsWith("/commands")) return Promise.resolve(Response.json([]));
    throw new Error(`Unexpected request: ${request.method} ${path}`);
  }));
  store.dispatch(connected("connection"));
  const oldQuery = store.dispatch(redpandaApi.endpoints.getConversation.initiate("Ada"));
  render(<Provider store={store}><MantineProvider env="test">
    <MemoryRouter initialEntries={["/sessions/Ada"]}>
      <nav><Link to="/sessions/Lin">打开 Lin</Link><Link to="/groups/team">打开群</Link></nav>
      <Routes>
        <Route path="/sessions/:sessionId" element={<SessionConversation />} />
        <Route path="/groups/:groupId" element={<GroupChatPage />} />
      </Routes>
    </MemoryRouter>
  </MantineProvider></Provider>);
  await waitFor(() => expect(selections).toContain("/api/sessions/Ada/select"));
  fireEvent.click(screen.getByRole("link", { name: "打开 Lin" }));
  await screen.findByText("来自 Lin 的消息");
  await waitFor(() => expect(store.getState().runtime.ownerSessionId).toBe("Lin"));
  await act(async () => {
    oldGet.resolve(result === "success" ? Response.json(snapshot("Ada")) : Response.json({ detail: "旧请求失败" }, { status: 500 }));
  });
  const settled = await oldQuery;
  expect(settled.isError).toBe(result === "failure");
  expect(screen.getByText("来自 Lin 的消息")).toBeInTheDocument();
  expect(screen.queryByText("来自 Ada 的消息")).not.toBeInTheDocument();
  expect(screen.queryByText("无法打开会话")).not.toBeInTheDocument();
  await act(async () => { oldSelect.resolve(Response.json(snapshot("Ada"))); });
  expect(store.getState().runtime.ownerSessionId).toBe("Lin");
  expect(selections.filter((path) => path.includes("/Lin/"))).toHaveLength(1);
  fireEvent.click(screen.getByRole("link", { name: "打开群" }));
  await screen.findByText("群里的消息");
  expect(screen.queryByText("来自 Lin 的消息")).not.toBeInTheDocument();
  expect(store.getState().runtime.viewingSessionId).toBeNull();
});

it.each(["success", "failure"])("切换群后，旧群请求%s不覆盖新群的消息和草稿", async (result) => {
  const store = createStore();
  stores.push(store);
  const oldResponse = deferred();
  const group = (id: string): GroupView => ({
    group_id: id, name: `${id}群`, members: ["Ada", "Lin"], dissolved: false, calling: [],
    messages: [{ message_id: `message-${id}`, sender: "Lin", text: `${id}群的消息`,
      mentioned_agents: [], reply_to: null, occurred_at: "2026-10-23T00:00:00Z" }],
  });
  vi.stubGlobal("fetch", vi.fn((request: Request) => {
    const path = new URL(request.url).pathname;
    if (path === "/api/groups/old") return oldResponse.promise;
    if (path === "/api/groups/new") return Promise.resolve(Response.json(group("new")));
    if (path === "/api/agents") return Promise.resolve(Response.json([]));
    throw new Error(`Unexpected request: ${request.method} ${path}`);
  }));
  store.dispatch(connected("connection"));
  const oldQuery = store.dispatch(redpandaApi.endpoints.getGroup.initiate("old"));
  render(<Provider store={store}><MantineProvider env="test">
    <MemoryRouter initialEntries={["/groups/old"]}>
      <Link to="/groups/new">切换群</Link>
      <Routes><Route path="/groups/:groupId" element={<GroupChatPage />} /></Routes>
    </MemoryRouter>
  </MantineProvider></Provider>);
  await screen.findByText("正在打开群聊…");
  fireEvent.click(screen.getByRole("link", { name: "切换群" }));
  await screen.findByText("new群的消息");
  fireEvent.change(screen.getByLabelText("群消息"), { target: { value: "新群草稿" } });
  await act(async () => {
    oldResponse.resolve(result === "success" ? Response.json(group("old")) : Response.json({ detail: "旧群失败" }, { status: 500 }));
    await oldQuery;
  });
  expect(screen.getByText("new群的消息")).toBeInTheDocument();
  expect(screen.getByLabelText("群消息")).toHaveValue("新群草稿");
  expect(screen.queryByText("old群的消息")).not.toBeInTheDocument();
  expect(screen.queryByText("这个群不存在或已无法读取。")).not.toBeInTheDocument();
});
