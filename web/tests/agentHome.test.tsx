import { MantineProvider } from "@mantine/core";
import { configureStore } from "@reduxjs/toolkit";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Provider } from "react-redux";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { AgentHome } from "../src/features/sessions/AgentHome";
import runtimeReducer, { connected } from "../src/realtime/runtimeSlice";

const queries = vi.hoisted(() => ({
  failed: false,
  createAgent: vi.fn(() => ({ unwrap: () => Promise.resolve({ session_id: "new-agent" }) })),
}));
vi.mock("../src/api/kondenApi", () => ({
  useGetWorkspacesQuery: () => ({ isError: queries.failed, data: queries.failed ? undefined : [
    { workspace_id: "alpha", name: "Alpha", task_root: "C:/alpha", full_access: false, created_at: "2026-10-20" },
    { workspace_id: "beta", name: "Beta", task_root: "C:/beta", full_access: false, created_at: "2026-10-20" },
  ] }),
  useGetAgentsQuery: () => ({ isError: queries.failed, data: [
    { workspace_id: "alpha", session_id: "a/1", name: "Ada", status: "idle" },
    { workspace_id: "beta", session_id: "b", name: "Lin", status: "idle" },
  ] }),
  useGetGroupsQuery: () => ({ data: [
    { workspace_id: "alpha", group_id: "g/1", name: "前端讨论", members: ["Ada"] },
    { workspace_id: "beta", group_id: "g2", name: "后端讨论", members: ["Lin"] },
  ] }),
  useGetInboxQuery: () => ({ data: [] }),
  useCreateGroupMutation: () => [vi.fn(), { reset: vi.fn() }],
  useCreateAgentMutation: () => [queries.createAgent, { isLoading: false, reset: vi.fn() }],
  useCreateWorkspaceMutation: () => [vi.fn(), { reset: vi.fn() }],
  useSelectWorkspaceDirectoryMutation: () => [vi.fn(), { reset: vi.fn() }],
}));

beforeEach(() => {
  queries.failed = false;
  queries.createAgent.mockClear();
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function renderHome(path = "/") {
  const store = configureStore({ reducer: { runtime: runtimeReducer } });
  store.dispatch(connected("connection"));
  render(<Provider store={store}><MantineProvider env="test"><MemoryRouter initialEntries={[path]}>
    <Routes><Route path="/" element={<AgentHome />} /><Route path="/workspaces/:workspaceId" element={<AgentHome />} />
      <Route path="/sessions/:sessionId" element={<p>新 Agent 对话</p>} /></Routes>
  </MemoryRouter></MantineProvider></Provider>);
}

it("首页显示所有 Bot 与群聊，并保留可编码的会话标识", () => {
  renderHome();
  expect(screen.getByRole("link", { name: /Ada/ })).toHaveAttribute("href", "/sessions/a%2F1");
  expect(screen.getByRole("link", { name: /前端讨论/ })).toHaveAttribute("href", "/groups/g%2F1");
  expect(screen.getByRole("link", { name: /Lin/ })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /后端讨论/ })).toBeInTheDocument();
});

it("新建 Bot 默认不关联代码项目，创建后进入对话", async () => {
  renderHome();
  fireEvent.click(screen.getByRole("button", { name: "新建 Bot" }));
  fireEvent.change(screen.getByRole("textbox", { name: /名字/ }), { target: { value: "Grace" } });
  fireEvent.click(screen.getByRole("button", { name: "创建", exact: true }));
  await waitFor(() => expect(queries.createAgent).toHaveBeenCalledWith({ connectionId: "connection", path: null, name: "Grace" }));
  expect(await screen.findByText("新 Agent 对话")).toBeInTheDocument();
});

it("名册查询失败时显示错误", () => {
  queries.failed = true;
  renderHome("/");
  expect(screen.getByText("工作台加载失败")).toBeInTheDocument();
  expect(screen.queryByText("从一个项目开始")).not.toBeInTheDocument();
});
