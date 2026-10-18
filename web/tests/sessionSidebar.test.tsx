import { AppShell, MantineProvider, Text } from "@mantine/core";
import { configureStore } from "@reduxjs/toolkit";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Provider } from "react-redux";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import type { AgentRosterEntry, GroupSummary, Workspace } from "../src/api/contracts";
import { SessionSidebar } from "../src/features/sessions/SessionSidebar";
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
  inbox: [] as { kind: "authorization"; label: string }[],
  workspaces: [] as Workspace[],
  deleteWorkspace: vi.fn(() => ({ unwrap: () => Promise.resolve() })),
  renameWorkspace: vi.fn(() => ({ unwrap: () => Promise.resolve() })),
  createAgent: vi.fn(() => ({ unwrap: () => Promise.resolve(agent({ session_id: "agent-new", name: "小熊猫" })) })),
  groups: [] as GroupSummary[],
  createGroup: vi.fn(() => ({ unwrap: () => Promise.resolve({ group_id: "group-new" }) })),
}));
vi.mock("../src/api/redpandaApi", () => ({
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
              <SessionSidebar onNavigate={() => {}} />
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

afterEach(() => { cleanup(); queries.inbox = []; vi.unstubAllGlobals(); });

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
      <SessionSidebar onNavigate={() => {}} />
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

it("移除工作区要先确认：取消不碰接口，确认才带上工作区 id", async () => {
  browserStubs();
  queries.deleteWorkspace.mockClear();
  queries.workspaces = [workspace("workspace", "工作区")];
  queries.agents = [];
  renderSidebar("/");

  fireEvent.click(screen.getByLabelText("工作区 工作区操作"));
  fireEvent.click(await screen.findByRole("menuitem", { name: "移除工作区" }));
  expect(screen.getByRole("dialog", { name: "移除工作区" })).toBeInTheDocument();
  expect(screen.getByText(/绑在它上面的 Agent 也不再列出/)).toBeInTheDocument();
  expect(screen.getByText(/不会把这些 Agent 认回来/)).toBeInTheDocument();
  expect(screen.queryByText(/它下面的会话/)).not.toBeInTheDocument();
  expect(queries.deleteWorkspace).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  await waitFor(() =>
    expect(screen.queryByRole("button", { name: "移除" })).toBeNull(),
  );
  expect(queries.deleteWorkspace).not.toHaveBeenCalled();

  fireEvent.click(screen.getByLabelText("工作区 工作区操作"));
  fireEvent.click(await screen.findByRole("menuitem", { name: "移除工作区" }));
  fireEvent.click(screen.getByRole("button", { name: "移除" }));
  await waitFor(() =>
    expect(queries.deleteWorkspace).toHaveBeenCalledWith({
      workspaceId: "workspace",
    }),
  );
  await waitFor(() =>
    expect(screen.queryByRole("button", { name: "移除" })).toBeNull(),
  );
});

it("移除正在聊天的工作区后，离开到剩下的工作区", async () => {
  browserStubs();
  queries.deleteWorkspace.mockClear();
  queries.workspaces = [workspace("workspace-a", "旧项目"), workspace("workspace-b", "新项目")];
  queries.agents = [agent({
    session_id: "agent-1", workspace_id: "workspace-a", name: "小熊猫",
  })];
  renderSidebar("/sessions/agent-1");
  expect(screen.getByTestId("current-path")).toHaveTextContent("/sessions/agent-1");

  fireEvent.click(screen.getByLabelText("旧项目 工作区操作"));
  fireEvent.click(await screen.findByRole("menuitem", { name: "移除工作区" }));
  fireEvent.click(screen.getByRole("button", { name: "移除" }));

  await waitFor(() =>
    expect(queries.deleteWorkspace).toHaveBeenCalledWith({
      workspaceId: "workspace-a",
    }),
  );
  await waitFor(() =>
    expect(screen.getByTestId("current-path")).toHaveTextContent("/workspaces/workspace-b"),
  );
});

it("右键和省略号打开同一组工作区操作，右键不改变折叠状态", async () => {
  browserStubs();
  queries.workspaces = [workspace("workspace", "项目")];
  queries.agents = [];
  renderSidebar("/");

  const toggle = screen.getByRole("button", { name: "项目" });
  const expanded = toggle.getAttribute("aria-expanded");
  fireEvent.contextMenu(toggle);
  expect(await screen.findByRole("menuitem", { name: "重命名工作区" })).toBeInTheDocument();
  expect(screen.queryByRole("menuitem", { name: "归档所有会话" })).not.toBeInTheDocument();
  expect(screen.getByRole("menuitem", { name: "移除工作区" })).toBeInTheDocument();
  expect(toggle).toHaveAttribute("aria-expanded", expanded);

  fireEvent.keyDown(screen.getByRole("menuitem", { name: "重命名工作区" }), { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
  fireEvent.click(screen.getByLabelText("项目 工作区操作"));
  expect(await screen.findByRole("menuitem", { name: "重命名工作区" })).toBeInTheDocument();
});

it("重命名回填当前名称，拒绝空白并只提交显示名称", async () => {
  browserStubs();
  queries.renameWorkspace.mockClear();
  queries.workspaces = [workspace("workspace", "旧名称")];
  queries.agents = [];
  renderSidebar("/");

  fireEvent.contextMenu(screen.getByRole("button", { name: "旧名称" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: "重命名工作区" }));
  expect(await screen.findByRole("textbox", { name: /名称/ })).toHaveValue("旧名称");
  fireEvent.change(screen.getByRole("textbox", { name: /名称/ }), { target: { value: "   " } });
  expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();
  fireEvent.change(screen.getByRole("textbox", { name: /名称/ }), { target: { value: " 新名称 " } });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(queries.renameWorkspace).toHaveBeenCalledWith({
    workspaceId: "workspace", name: "新名称",
  }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
});

it("点 Agent 打开它的会话", () => {
  browserStubs();
  queries.workspaces = [workspace("workspace", "项目")];
  queries.agents = [agent({ session_id: "agent-1", name: "小熊猫" })];
  renderSidebar("/");
  fireEvent.click(screen.getByRole("button", { name: "小熊猫" }));
  expect(screen.getByTestId("current-path")).toHaveTextContent("/sessions/agent-1");
});

it("待处理按钮显示收件箱条数并进入收件箱", () => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  queries.inbox = [{ kind: "authorization", label: "验证熊猫" }, { kind: "authorization", label: "验证熊猫" }];
  renderSidebar("/");

  fireEvent.click(screen.getByRole("button", { name: "待处理 · 2" }));
  expect(screen.getByTestId("current-path")).toHaveTextContent("/inbox");
});

it("群聊列在所属项目下：点开进入群，新建群只能从同项目 Agent 里选成员", async () => {
  browserStubs();
  queries.workspaces = [workspace("workspace", "工作区"), workspace("other", "别处")];
  queries.agents = [
    agent({ session_id: "a", name: "Ada" }),
    agent({ session_id: "l", name: "Lin" }),
    agent({ session_id: "x", name: "外人", workspace_id: "other" }),
  ];
  queries.groups = [{ group_id: "group-1", workspace_id: "workspace", name: "开发组", members: ["Ada", "Lin"] }];
  renderSidebar("/");

  fireEvent.click(screen.getByLabelText("群聊 开发组"));
  expect(screen.getByTestId("current-path")).toHaveTextContent("/groups/group-1");

  fireEvent.click(screen.getByLabelText("工作区 工作区操作"));
  fireEvent.click(await screen.findByRole("menuitem", { name: "新建群聊" }));
  const dialog = screen.getByRole("dialog", { name: "新建群聊" });
  fireEvent.change(screen.getByLabelText("群名"), { target: { value: "评审组" } });
  fireEvent.click(screen.getByRole("combobox", { name: "成员" }));
  expect(screen.queryByRole("option", { name: "外人" })).not.toBeInTheDocument();
  const create = screen.getByRole("button", { name: "创建" });
  fireEvent.click(await screen.findByRole("option", { name: "Ada" }));
  expect(create).toBeDisabled();
  fireEvent.click(screen.getByRole("combobox", { name: "成员" }));
  fireEvent.click(await screen.findByRole("option", { name: "Lin" }));
  fireEvent.click(create);
  await waitFor(() => expect(queries.createGroup).toHaveBeenCalledWith({
    connectionId: "connection", workspaceId: "workspace", name: "评审组", members: ["Ada", "Lin"],
  }));
  await waitFor(() => expect(screen.getByTestId("current-path")).toHaveTextContent("/groups/group-new"));
  expect(dialog).not.toBeInTheDocument();
});
