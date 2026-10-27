import { AppShell, MantineProvider, Text } from "@mantine/core";
import { configureStore } from "@reduxjs/toolkit";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { Provider } from "react-redux";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import type { AgentRosterEntry, ExternalSessionSummary, GroupSummary, Workspace } from "../src/api/contracts";
import { AgentSidebar } from "../src/features/sessions/AgentSidebar";
import runtimeReducer, { connected, outputFinal, sessionActivity, viewing } from "../src/realtime/runtimeSlice";

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
    activity: "idle",
    status: "idle",
    ...partial,
  };
}

const queries = vi.hoisted(() => ({
  agents: [] as AgentRosterEntry[],
  externalSessions: [] as ExternalSessionSummary[],
  inbox: [] as { kind: "authorization"; label: string; session_id: string }[],
  workspaces: [] as Workspace[],
  deleteWorkspace: vi.fn(() => ({ unwrap: () => Promise.resolve() })),
  renameWorkspace: vi.fn(() => ({ unwrap: () => Promise.resolve() })),
  createAgent: vi.fn(() => ({ unwrap: () => Promise.resolve(agent({ session_id: "agent-new", name: "konden" })) })),
  groups: [] as GroupSummary[],
  setAutoAuthorize: vi.fn(),
  createGroup: vi.fn(() => ({ unwrap: () => Promise.resolve({ group_id: "group-new" }) })),
  deleteAgent: vi.fn(() => ({ unwrap: () => Promise.resolve() })),
  dissolveGroup: vi.fn(() => ({ unwrap: () => Promise.resolve() })),
}));
vi.mock("../src/api/kondenApi", () => ({
  useDeleteAgentMutation: () => [queries.deleteAgent, { isLoading: false }],
  useDissolveGroupMutation: () => [queries.dissolveGroup, { isLoading: false }],
  useGetAutoAuthorizeQuery: () => ({ data: { enabled: false } }),
  useSetAutoAuthorizeMutation: () => [queries.setAutoAuthorize, { isLoading: false }],
  useGetAgentsQuery: () => ({ data: queries.agents, isLoading: false }),
  useGetExternalSessionsQuery: () => ({ data: queries.externalSessions, isLoading: false }),
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

it("能力入口直接从侧栏打开，并标记当前页面", () => {
  browserStubs();
  queries.agents = []; queries.groups = [];
  renderSidebar("/");
  fireEvent.click(screen.getByRole("button", { name: "能力", exact: true }));
  expect(screen.getByTestId("current-path")).toHaveTextContent("/capabilities");
  expect(screen.getByRole("button", { name: "能力", exact: true })).toHaveAttribute("aria-current", "page");
});

afterEach(() => {
  cleanup(); queries.inbox = []; queries.externalSessions = []; vi.unstubAllGlobals();
  Object.keys(window.localStorage).filter((key) => key.startsWith("konden.sidebarGroups")).forEach((key) => window.localStorage.removeItem(key));
});

it("没有内部 Bot 时仍能找到并打开已接入的外部会话", () => {
  browserStubs();
  queries.agents = []; queries.groups = [];
  queries.externalSessions = [{ identity: "1".repeat(32), name: "外部会话-11111111", active: true,
    created_at: "2026-10-25T01:00:00Z", last_activity_at: "2026-10-25T01:00:00Z", peers: [] }];
  renderSidebar("/");
  const external = within(screen.getByRole("region", { name: "外部会话" }));
  fireEvent.click(external.getByRole("button", { name: "外部会话-11111111" }));
  expect(screen.getByTestId("current-path")).toHaveTextContent(`/external/${"1".repeat(32)}`);
  expect(external.getByRole("button", { name: "外部会话-11111111" })).toHaveAttribute("aria-current", "page");
});

it("删除 Bot 要确认，失败保留弹窗，成功后离开被删除的会话", async () => {
  browserStubs();
  queries.agents = [agent({ session_id: "ada", name: "Ada" })];
  queries.groups = [];
  queries.deleteAgent.mockClear();
  renderSidebar("/sessions/ada");
  fireEvent.click(screen.getByRole("button", { name: "Ada 操作" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: "删除 Bot" }));
  expect(queries.deleteAgent).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(queries.deleteAgent).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Ada 操作" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: "删除 Bot" }));
  queries.deleteAgent.mockReturnValueOnce({ unwrap: () => Promise.reject("停止失败，请重试。") });
  fireEvent.click(screen.getByRole("button", { name: "删除 Bot", exact: true }));
  expect(await screen.findByRole("alert")).toHaveTextContent("停止失败，请重试。");
  expect(screen.getByTestId("current-path")).toHaveTextContent("/sessions/ada");
  fireEvent.click(screen.getByRole("button", { name: "删除 Bot", exact: true }));
  await waitFor(() => expect(screen.getByTestId("current-path")).toHaveTextContent(/^\/$/));
  expect(queries.deleteAgent).toHaveBeenLastCalledWith({ connectionId: "connection", sessionId: "ada" });
});

it("解散其他群聊不离开当前 Bot 会话", async () => {
  browserStubs();
  queries.agents = [agent({ session_id: "ada", name: "Ada" })];
  queries.groups = [{ group_id: "team", name: "开发群", members: ["Ada", "Lin"] }];
  queries.dissolveGroup.mockClear();
  renderSidebar("/sessions/ada");
  fireEvent.click(screen.getByRole("button", { name: "群聊 开发群 操作" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: "解散群聊" }));
  expect(queries.dissolveGroup).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "解散群聊", exact: true }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(queries.dissolveGroup).toHaveBeenCalledWith({ connectionId: "connection", groupId: "team" });
  expect(screen.getByTestId("current-path")).toHaveTextContent("/sessions/ada");
});

it("Bot 和群聊可混放分组，折叠与归属刷新保留，删除分组不删除条目", async () => {
  browserStubs();
  vi.stubGlobal("crypto", { getRandomValues: crypto.getRandomValues.bind(crypto) });
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
  fireEvent.click(screen.getByRole("button", { name: "群聊 开发群 操作" }));
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
  window.localStorage.setItem("konden.sidebarGroups", JSON.stringify(stored));
  renderSidebar("/");
  expect(within(screen.getByRole("region", { name: "名册的分组 未分组" })).getByRole("button", { name: "Lin", exact: true })).toBeInTheDocument();
  fireEvent.change(screen.getByRole("textbox", { name: "搜索对话" }), { target: { value: "Ada" } });
  expect(screen.getByRole("button", { name: "分组 研发" })).toHaveAttribute("aria-expanded", "true");
  expect(await screen.findByRole("button", { name: "Ada", exact: true })).toBeInTheDocument();
  expect(JSON.parse(window.localStorage.getItem("konden.sidebarGroups")!).sections[0].collapsed).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "清除搜索" }));
  expect(screen.getByRole("button", { name: "分组 研发" })).toHaveAttribute("aria-expanded", "false");
});

it("名册用干活、等授权、等人回话和空闲标出状态，运行中的活动盖过空闲", () => {
  browserStubs();
  queries.workspaces = [workspace("workspace", "工作区")];
  queries.agents = [agent({
    session_id: "parent", name: "konden", status: "working", activity: "idle",
  })];
  const store = configureStore({ reducer: { runtime: runtimeReducer } });
  store.dispatch(sessionActivity({ sessionId: "parent", activity: "idle" }));
  const sidebar = () => <Provider store={store}><MantineProvider><MemoryRouter>
    <AppShell navbar={{ width: 240, breakpoint: 0 }}><AppShell.Navbar>
      <AgentSidebar onNavigate={() => {}} />
    </AppShell.Navbar></AppShell>
  </MemoryRouter></MantineProvider></Provider>;
  const view = render(sidebar());
  expect(screen.getByLabelText("干活")).toBeInTheDocument();

  queries.agents = [agent({ session_id: "parent", name: "konden", status: "idle" })];
  view.rerender(sidebar());
  expect(screen.getByLabelText("空闲")).toBeInTheDocument();

  act(() => { store.dispatch(sessionActivity({ sessionId: "parent", activity: "running" })); });
  expect(screen.getByLabelText("干活")).toBeInTheDocument();

  queries.agents = [agent({ session_id: "parent", name: "konden", status: "authorization" })];
  view.rerender(sidebar());
  expect(screen.getByLabelText("等授权")).toBeInTheDocument();
  act(() => { store.dispatch(viewing("parent")); });
  expect(screen.getByLabelText("等授权")).toBeInTheDocument();

  act(() => { store.dispatch(sessionActivity({ sessionId: "parent", activity: "idle" })); });
  queries.agents = [agent({ session_id: "parent", name: "konden", status: "waiting" })];
  view.rerender(sidebar());
  expect(screen.getByLabelText("等人回话")).toBeInTheDocument();
});

it("点 Agent 打开它的会话", () => {
  browserStubs();
  queries.workspaces = [workspace("workspace", "项目")];
  queries.agents = [agent({ session_id: "agent-1", name: "konden" })];
  renderSidebar("/");
  fireEvent.click(screen.getByRole("button", { name: "konden" }));
  expect(screen.getByTestId("current-path")).toHaveTextContent("/sessions/agent-1");
});

it("离开的 Bot 收到回复显示未读红标，查看后清除，状态提示保持独立", () => {
  browserStubs();
  queries.agents = [agent({ session_id: "ada", name: "Ada", status: "working" }), agent({ session_id: "lin", name: "Lin" })];
  queries.groups = [];
  const { store } = renderSidebar("/sessions/lin");
  act(() => {
    store.dispatch(viewing("lin"));
    store.dispatch(outputFinal({ sessionId: "ada", outputId: "reply-1", text: "下载完成" }));
    store.dispatch(outputFinal({ sessionId: "ada", outputId: "reply-2", text: "校验完成" }));
    store.dispatch(outputFinal({ sessionId: "lin", outputId: "reply-3", text: "正在查看" }));
  });
  const ada = within(screen.getByRole("button", { name: "Ada", exact: true }));
  expect(ada.getByRole("status", { name: "Ada 有 2 条未读消息" })).toHaveTextContent("2");
  expect(ada.getByLabelText("干活")).toBeInTheDocument();
  expect(within(screen.getByRole("button", { name: "Lin", exact: true })).queryByRole("status")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Ada", exact: true }));
  act(() => { store.dispatch(viewing("ada")); });
  expect(ada.queryByRole("status")).not.toBeInTheDocument();
});

it("对话入口返回首页，待处理提示所属 Bot", () => {
  browserStubs();
  queries.agents = [agent({ session_id: "ada", name: "Ada" }), agent({ session_id: "lin", name: "Lin" })];
  queries.groups = [];
  queries.inbox = [{ kind: "authorization", label: "Ada", session_id: "ada" }];
  renderSidebar("/sessions/ada");

  expect(within(screen.getByRole("button", { name: "Ada", exact: true })).getByLabelText("空闲 · 需要你处理")).toBeInTheDocument();
  expect(within(screen.getByRole("button", { name: "Lin", exact: true })).getByLabelText("空闲")).toBeInTheDocument();
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
