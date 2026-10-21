import { AppShell, MantineProvider, Text } from "@mantine/core";
import { configureStore } from "@reduxjs/toolkit";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { Provider } from "react-redux";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import type { AgentRosterEntry, GroupSummary, Workspace } from "../src/api/contracts";
import { AgentSidebar } from "../src/features/sessions/AgentSidebar";
import runtimeReducer, { connected, sessionActivity, viewing } from "../src/realtime/runtimeSlice";

function workspace(workspaceId: string, name: string): Workspace {
  return {
    workspace_id: workspaceId,
    name,
    task_root: `D:\\项目\\${name}`,
    full_access: false,
    created_at: "2026-01-01T00:00:00+00:00",
  };
}

function agent(partial: Partial<AgentRosterEntry> & Pick<AgentRosterEntry, "session_id" | "name">): AgentRosterEntry {
  return {
    workspace_id: "workspace",
    direction: null,
    activity: "idle",
    status: "idle",
    ...partial,
  };
}

const queries = vi.hoisted(() => ({
  agents: [] as AgentRosterEntry[],
  inbox: [] as { kind: "authorization"; label: string; session_id: string }[],
  workspaces: [] as Workspace[],
  deleteWorkspace: vi.fn(() => ({ unwrap: () => Promise.resolve() })),
  renameWorkspace: vi.fn(() => ({ unwrap: () => Promise.resolve() })),
  createAgent: vi.fn(() => ({ unwrap: () => Promise.resolve(agent({ session_id: "agent-new", name: "小熊猫" })) })),
  groups: [] as GroupSummary[],
  setAutoAuthorize: vi.fn(),
  createGroup: vi.fn(() => ({ unwrap: () => Promise.resolve({ group_id: "group-new" }) })),
}));
vi.mock("../src/api/redpandaApi", () => ({
  useGetAutoAuthorizeQuery: () => ({ data: { enabled: false } }),
  useSetAutoAuthorizeMutation: () => [queries.setAutoAuthorize, { isLoading: false }],
  useGetAgentsQuery: () => ({ data: queries.agents, isLoading: false }),
  useGetInboxQuery: () => ({ data: queries.inbox, isLoading: false }),
  useGetGroupsQuery: () => ({ data: queries.groups, isLoading: false }),
  useCreateGroupMutation: () => [queries.createGroup, { isLoading: false, reset: vi.fn() }],
  useGetWorkspacesQuery: () => ({ data: queries.workspaces, isLoading: false }),
  useCreateAgentMutation: () => [queries.createAgent, { isLoading: false, reset: vi.fn() }],
  useCreateWorkspaceMutation: () => [vi.fn(), {}],
  useSelectWorkspaceDirectoryMutation: () => [vi.fn(), {}],
  useDeleteWorkspaceMutation: () => [
    queries.deleteWorkspace,
    { isLoading: false, isError: false, reset: vi.fn() },
  ],
  useRenameWorkspaceMutation: () => [
    queries.renameWorkspace,
    { isLoading: false, isError: false },
  ],
}));

function CurrentPath() {
  return <Text data-testid="current-path">{useLocation().pathname}</Text>;
}

function renderSidebar(initialEntry: string) {
  const store = configureStore({ reducer: { runtime: runtimeReducer } });
  store.dispatch(connected("connection"));
  const view = render(
    <Provider store={store}>
      <MantineProvider env="test">
        <MemoryRouter initialEntries={[initialEntry]}>
          <Routes>
            <Route path="/sessions/:sessionId" element={null} />
            <Route path="/workspaces/:workspaceId" element={null} />
            <Route path="*" element={null} />
          </Routes>
          <AppShell navbar={{ width: 240, breakpoint: 0 }}>
            <AppShell.Navbar>
              <CurrentPath />
              <AgentSidebar onNavigate={() => {}} />
            </AppShell.Navbar>
          </AppShell>
        </MemoryRouter>
      </MantineProvider>
    </Provider>,
  );
  return { ...view, store };
}

function browserStubs() {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
}

afterEach(() => {
  cleanup(); queries.inbox = []; vi.unstubAllGlobals();
  Object.keys(window.localStorage).filter((key) => key.startsWith("redpanda.sidebarGroups")).forEach((key) => window.localStorage.removeItem(key));
});

it("Bot 和群聊可混放分组，折叠与归属刷新保留，删除分组不删除条目", async () => {
  browserStubs();
  queries.workspaces = [workspace("organize", "项目")];
  queries.agents = [agent({ session_id: "ada", name: "Ada", workspace_id: "organize" })];
  queries.groups = [{ group_id: "chat", name: "开发群", members: ["Ada", "Lin"] }];
  renderSidebar("/sessions/ada");
  fireEvent.click(screen.getByRole("button", { name: "新建分组" }));
  fireEvent.change(screen.getByLabelText("分组名称"), { target: { value: "研发" } });
  fireEvent.click(screen.getByRole("button", { name: "创建", exact: true }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

  fireEvent.contextMenu(screen.getByRole("button", { name: "Ada", exact: true }));
  fireEvent.click(await screen.findByRole("menuitem", { name: "研发", exact: true }));
  fireEvent.click(screen.getByRole("button", { name: "移动 群聊 开发群 到分组" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: "研发", exact: true }));
  const section = screen.getByRole("region", { name: "名册的分组 研发" });
  expect(within(section).getByRole("button", { name: "Ada", exact: true })).toHaveAttribute("aria-current", "page");
  fireEvent.click(within(section).getByRole("button", { name: "群聊 开发群", exact: true }));
  expect(screen.getByTestId("current-path")).toHaveTextContent("/groups/chat");
  fireEvent.click(screen.getByRole("button", { name: "分组 研发", exact: true }));
  expect(screen.getByRole("button", { name: "分组 研发", exact: true })).toHaveAttribute("aria-expanded", "false");
  cleanup();
  renderSidebar("/groups/chat");
  expect(screen.getByRole("button", { name: "分组 研发", exact: true })).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(screen.getByRole("button", { name: "研发 分组操作" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: "重命名分组" }));
  fireEvent.change(screen.getByLabelText("分组名称"), { target: { value: "开发" } });
  fireEvent.click(screen.getByRole("button", { name: "保存", exact: true }));
  await waitFor(() => expect(screen.getByRole("button", { name: "分组 开发", exact: true })).toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: "开发 分组操作" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: "删除分组" }));
  const ungrouped = screen.getByRole("region", { name: "名册的分组 未分组" });
  expect(await within(ungrouped).findByRole("button", { name: "Ada", exact: true })).toBeInTheDocument();
  expect(within(ungrouped).getByRole("button", { name: "群聊 开发群", exact: true })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "分组 开发", exact: true })).not.toBeInTheDocument();
});

it("名册分组覆盖所有 Bot，搜索展开但不改写折叠偏好", async () => {
  browserStubs();
  queries.workspaces = [workspace("organize", "项目"), workspace("other", "别处")];
  queries.agents = [agent({ session_id: "ada", name: "Ada", workspace_id: "organize" }), agent({ session_id: "lin", name: "Lin", workspace_id: "other" })];
  queries.groups = [];
  const stored = { sections: [{ id: "dev", name: "研发", collapsed: true }], assignments: { "agent:ada": "dev" }, ungroupedCollapsed: false };
  window.localStorage.setItem("redpanda.sidebarGroups", JSON.stringify(stored));
  renderSidebar("/");
  expect(within(screen.getByRole("region", { name: "名册的分组 未分组" })).getByRole("button", { name: "Lin", exact: true })).toBeInTheDocument();
  fireEvent.change(screen.getByRole("textbox", { name: "搜索 Bot 或群聊" }), { target: { value: "Ada" } });
  expect(screen.getByRole("button", { name: "分组 研发" })).toHaveAttribute("aria-expanded", "true");
  expect(await screen.findByRole("button", { name: "Ada", exact: true })).toBeInTheDocument();
  expect(JSON.parse(window.localStorage.getItem("redpanda.sidebarGroups")!).sections[0].collapsed).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "清除搜索" }));
  expect(screen.getByRole("button", { name: "分组 研发" })).toHaveAttribute("aria-expanded", "false");
});

it("名册用干活、等授权、等人回话和空闲标出状态，运行中的活动盖过空闲", () => {
  browserStubs();
  queries.workspaces = [workspace("workspace", "工作区")];
  queries.agents = [agent({
    session_id: "parent", name: "小熊猫", status: "working", activity: "idle",
  })];
  const store = configureStore({ reducer: { runtime: runtimeReducer } });
  store.dispatch(sessionActivity({ sessionId: "parent", activity: "idle" }));
  const sidebar = () => <Provider store={store}><MantineProvider><MemoryRouter>
    <AppShell navbar={{ width: 240, breakpoint: 0 }}><AppShell.Navbar>
      <AgentSidebar onNavigate={() => {}} />
    </AppShell.Navbar></AppShell>
  </MemoryRouter></MantineProvider></Provider>;
  const view = render(sidebar());
  expect(screen.getByLabelText("干活")).toHaveClass("activity-dot-running");

  queries.agents = [agent({ session_id: "parent", name: "小熊猫", status: "idle" })];
  view.rerender(sidebar());
  expect(screen.getByLabelText("空闲")).toHaveClass("activity-dot-idle");

  act(() => { store.dispatch(sessionActivity({ sessionId: "parent", activity: "running" })); });
  expect(screen.getByLabelText("干活")).toHaveClass("activity-dot-running");

  queries.agents = [agent({ session_id: "parent", name: "小熊猫", status: "authorization" })];
  view.rerender(sidebar());
  expect(screen.getByLabelText("等授权")).toHaveClass("activity-dot-attention");
  act(() => { store.dispatch(viewing("parent")); });
  expect(screen.getByLabelText("等授权")).toHaveClass("activity-dot-attention");

  act(() => { store.dispatch(sessionActivity({ sessionId: "parent", activity: "idle" })); });
  queries.agents = [agent({ session_id: "parent", name: "小熊猫", status: "waiting", direction: "前端" })];
  view.rerender(sidebar());
  expect(screen.getByLabelText("等人回话")).toHaveClass("activity-dot-idle");
  expect(screen.getByText("前端")).toBeInTheDocument();
});

it("点 Agent 打开它的会话", () => {
  browserStubs();
  queries.workspaces = [workspace("workspace", "项目")];
  queries.agents = [agent({ session_id: "agent-1", name: "小熊猫" })];
  renderSidebar("/");
  fireEvent.click(screen.getByRole("button", { name: "小熊猫" }));
  expect(screen.getByTestId("current-path")).toHaveTextContent("/sessions/agent-1");
});

it("对话入口返回首页，待处理只提示所属 Bot，不占独立导航", () => {
  browserStubs();
  queries.agents = [agent({ session_id: "ada", name: "Ada" }), agent({ session_id: "lin", name: "Lin" })];
  queries.groups = [];
  queries.inbox = [{ kind: "authorization", label: "Ada", session_id: "ada" }];
  renderSidebar("/sessions/ada");

  expect(screen.queryByRole("button", { name: "工作台" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /^待处理/ })).not.toBeInTheDocument();
  expect(within(screen.getByRole("button", { name: "Ada", exact: true })).getByLabelText("空闲 · 需要你处理")).toHaveClass("activity-dot-attention");
  expect(within(screen.getByRole("button", { name: "Lin", exact: true })).getByLabelText("空闲")).toHaveClass("activity-dot-idle");
  fireEvent.click(screen.getByRole("button", { name: "所有对话" }));
  expect(screen.getByTestId("current-path")).toHaveTextContent(/^\/$/);
});

it("群聊直接列在名册中，成员可选任意 Bot", async () => {
  browserStubs();
  queries.workspaces = [workspace("workspace", "工作区"), workspace("other", "别处")];
  queries.agents = [
    agent({ session_id: "a", name: "Ada" }),
    agent({ session_id: "l", name: "Lin" }),
    agent({ session_id: "x", name: "外人", workspace_id: "other" }),
  ];
  queries.groups = [{ group_id: "group-1", name: "开发组", members: ["Ada", "Lin"] }];
  renderSidebar("/");

  fireEvent.click(screen.getByLabelText("群聊 开发组"));
  expect(screen.getByTestId("current-path")).toHaveTextContent("/groups/group-1");

  fireEvent.click(screen.getByRole("button", { name: "新建群聊" }));
  const dialog = screen.getByRole("dialog", { name: "新建群聊" });
  fireEvent.change(screen.getByLabelText("群名"), { target: { value: "评审组" } });
  fireEvent.click(screen.getByRole("combobox", { name: "成员" }));
  expect(screen.getByRole("option", { name: "外人" })).toBeInTheDocument();
  const create = screen.getByRole("button", { name: "创建" });
  fireEvent.click(await screen.findByRole("option", { name: "Ada" }));
  expect(create).toBeDisabled();
  fireEvent.click(screen.getByRole("combobox", { name: "成员" }));
  fireEvent.click(await screen.findByRole("option", { name: "Lin" }));
  fireEvent.click(create);
  await waitFor(() => expect(queries.createGroup).toHaveBeenCalledWith({
    connectionId: "connection", name: "评审组", members: ["Ada", "Lin"],
  }));
  await waitFor(() => expect(screen.getByTestId("current-path")).toHaveTextContent("/groups/group-new"));
  expect(dialog).not.toBeInTheDocument();
});

it("侧栏自动放行提交全局设置，返回首页仍显示开关", async () => {
  browserStubs();
  queries.agents = [agent({ session_id: "ada", name: "Ada" })];
  queries.groups = [];
  queries.setAutoAuthorize.mockClear();
  renderSidebar("/sessions/ada");
  fireEvent.click(screen.getByRole("switch", { name: "自动放行写文件等工具" }));
  expect(queries.setAutoAuthorize).toHaveBeenCalledWith({ connectionId: "connection", enabled: true });
  fireEvent.click(screen.getByRole("button", { name: "所有对话" }));
  expect(screen.getByRole("switch", { name: "自动放行写文件等工具" })).toBeInTheDocument();
});
