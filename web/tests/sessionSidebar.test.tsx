import { AppShell, MantineProvider, Text } from "@mantine/core";
import { configureStore } from "@reduxjs/toolkit";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Provider } from "react-redux";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import type { SessionSummary, Workspace } from "../src/api/contracts";
import { SessionSidebar } from "../src/features/sessions/SessionSidebar";
import runtimeReducer, { connected, hydrateDrafts, outputFinal, sessionActivity, viewing } from "../src/realtime/runtimeSlice";

function workspace(workspaceId: string, name: string): Workspace {
  return {
    workspace_id: workspaceId,
    name,
    task_root: `D:\\项目\\${name}`,
    full_access: false,
    created_at: "2026-01-01T00:00:00+00:00",
  };
}

const queries = vi.hoisted(() => ({
  sessions: [] as SessionSummary[],
  inbox: [] as { kind: "authorization"; label: string }[],
  workspaces: [] as Workspace[],
  deleteWorkspace: vi.fn(() => ({ unwrap: () => Promise.resolve() })),
  renameWorkspace: vi.fn(() => ({ unwrap: () => Promise.resolve() })),
  archiveSession: vi.fn((_arg: { connectionId: string; sessionId: string }) => ({ unwrap: () => Promise.resolve() })),
}));
vi.mock("../src/api/redpandaApi", () => ({
  useGetSessionsQuery: () => ({ data: queries.sessions, isLoading: false }),
  useGetInboxQuery: () => ({ data: queries.inbox, isLoading: false }),
  useGetSessionTitlesQuery: () => ({ data: {} }),
  useGetWorkspacesQuery: () => ({ data: queries.workspaces, isLoading: false }),
  useArchiveSessionMutation: () => [queries.archiveSession],
  useSetSessionTitleMutation: () => [vi.fn()],
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

function renderSidebar(initialEntry: string, drafts: Record<string, string> = {}) {
  const store = configureStore({ reducer: { runtime: runtimeReducer } });
  store.dispatch(connected("connection"));
  store.dispatch(hydrateDrafts(drafts));
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

afterEach(() => { cleanup(); queries.inbox = []; vi.unstubAllGlobals(); });

it("侧栏区分执行、待操作和结束，后台 step 输出仍保持旋钮", () => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  queries.workspaces = [workspace("workspace", "工作区")];
  queries.sessions = [{
    session_id: "parent", workspace_id: "workspace", title: "父会话",
    updated_at: null, activity: "idle", has_active_subagents: true, needs_attention: false,
  }];
  const store = configureStore({ reducer: { runtime: runtimeReducer } });
  store.dispatch(sessionActivity({ sessionId: "parent", activity: "idle" }));
  const sidebar = () => <Provider store={store}><MantineProvider><MemoryRouter>
    <AppShell navbar={{ width: 240, breakpoint: 0 }}><AppShell.Navbar>
      <SessionSidebar onNavigate={() => {}} />
    </AppShell.Navbar></AppShell>
  </MemoryRouter></MantineProvider></Provider>;
  const view = render(sidebar());
  expect(screen.getByLabelText("等待子 Agent")).toHaveClass("activity-dot-running");
  expect(store.getState().runtime.sessions.parent.activity).toBe("idle");

  queries.sessions = [{ ...queries.sessions[0], has_active_subagents: false }];
  view.rerender(sidebar());
  expect(screen.getByLabelText("空闲")).toHaveClass("activity-dot-idle");

  act(() => { store.dispatch(sessionActivity({ sessionId: "parent", activity: "running" })); });
  expect(screen.getByLabelText("运行中")).toHaveClass("activity-dot-running");

  act(() => {
    store.dispatch(viewing("other-session"));
    store.dispatch(outputFinal({ sessionId: "parent", outputId: "step-1", text: "继续调查" }));
    store.dispatch(outputFinal({ sessionId: "parent", outputId: "step-2", text: "调用工具" }));
  });
  expect(screen.getByLabelText("运行中")).toHaveClass("activity-dot-running");
  expect(screen.queryByText("2")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("需要操作")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("执行已结束，有新结果")).not.toBeInTheDocument();

  queries.sessions = [{ ...queries.sessions[0], needs_attention: true }];
  view.rerender(sidebar());
  expect(screen.getByLabelText("需要操作")).toHaveClass("activity-dot-attention");
  // 看过会话也不能清掉尚未处理的确认。
  act(() => { store.dispatch(viewing("parent")); });
  expect(screen.getByLabelText("需要操作")).toHaveClass("activity-dot-attention");

  queries.sessions = [{ ...queries.sessions[0], needs_attention: false }];
  view.rerender(sidebar());
  expect(screen.getByLabelText("运行中")).toHaveClass("activity-dot-running");
  act(() => {
    store.dispatch(viewing("other-session"));
    store.dispatch(outputFinal({ sessionId: "parent", outputId: "final", text: "完成" }));
    store.dispatch(sessionActivity({ sessionId: "parent", activity: "idle" }));
  });
  expect(screen.getByLabelText("执行已结束，有新结果")).toHaveClass("activity-dot-completed");
  act(() => { store.dispatch(viewing("parent")); });
  expect(screen.getByLabelText("空闲")).toHaveClass("activity-dot-idle");
});

it("移除工作区要先确认：取消不碰接口，确认才带上工作区 id", async () => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  queries.deleteWorkspace.mockClear();
  queries.workspaces = [workspace("workspace", "工作区")];
  queries.sessions = [];
  renderSidebar("/");

  fireEvent.click(screen.getByLabelText("工作区 工作区操作"));
  fireEvent.click(await screen.findByRole("menuitem", { name: "移除工作区" }));
  expect(screen.getByRole("dialog", { name: "移除工作区" })).toBeInTheDocument();
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
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  queries.deleteWorkspace.mockClear();
  queries.workspaces = [workspace("workspace-a", "旧项目"), workspace("workspace-b", "新项目")];
  queries.sessions = [{
    session_id: "session-1", workspace_id: "workspace-a", title: "旧会话",
    updated_at: null, activity: "idle", has_active_subagents: false, needs_attention: false,
  }];
  renderSidebar("/sessions/session-1");
  expect(screen.getByTestId("current-path")).toHaveTextContent("/sessions/session-1");

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
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  queries.workspaces = [workspace("workspace", "项目")];
  queries.sessions = [];
  renderSidebar("/");

  const toggle = screen.getByRole("button", { name: "项目" });
  const expanded = toggle.getAttribute("aria-expanded");
  fireEvent.contextMenu(toggle);
  expect(await screen.findByRole("menuitem", { name: "重命名工作区" })).toBeInTheDocument();
  expect(screen.getByRole("menuitem", { name: "归档所有会话" })).toBeDisabled();
  expect(screen.getByRole("menuitem", { name: "移除工作区" })).toBeInTheDocument();
  expect(toggle).toHaveAttribute("aria-expanded", expanded);
  expect(screen.queryByLabelText("删除 项目")).not.toBeInTheDocument();

  fireEvent.keyDown(screen.getByRole("menuitem", { name: "重命名工作区" }), { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
  fireEvent.click(screen.getByLabelText("项目 工作区操作"));
  expect(await screen.findByRole("menuitem", { name: "重命名工作区" })).toBeInTheDocument();
});

it("重命名回填当前名称，拒绝空白并只提交显示名称", async () => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  queries.renameWorkspace.mockClear();
  queries.workspaces = [workspace("workspace", "旧名称")];
  queries.sessions = [];
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

it("归档该工作区全部会话，含隐藏项与草稿，离开已归档的当前会话", async () => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  queries.archiveSession.mockClear();
  queries.workspaces = [workspace("workspace-a", "项目 A"), workspace("workspace-b", "项目 B")];
  queries.sessions = Array.from({ length: 7 }, (_, index) => ({
    session_id: `session-${index}`, workspace_id: "workspace-a", title: `会话 ${index}`,
    updated_at: null, activity: "idle", has_active_subagents: false, needs_attention: false,
  }));
  queries.sessions.push({ ...queries.sessions[0], session_id: "other", workspace_id: "workspace-b" });
  const { store } = renderSidebar("/sessions/session-0", { "workspace-a": "draft-a", "workspace-b": "draft-b" });
  expect(screen.queryByText("会话 6")).not.toBeInTheDocument();

  fireEvent.contextMenu(screen.getByRole("button", { name: "项目 A" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: "归档所有会话" }));
  await waitFor(() => expect(queries.archiveSession).toHaveBeenCalledTimes(8));
  expect(queries.archiveSession.mock.calls.map(([arg]) => arg)).toEqual([
    ...Array.from({ length: 7 }, (_, index) => ({ connectionId: "connection", sessionId: `session-${index}` })),
    { connectionId: "connection", sessionId: "draft-a" },
  ]);
  await waitFor(() => expect(store.getState().runtime.draftSessions).toEqual({ "workspace-b": "draft-b" }));
  await waitFor(() => expect(screen.getByTestId("current-path")).toHaveTextContent("/workspaces/workspace-a"));
});

it("归档失败保留未成功的当前会话并显示错误", async () => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  queries.archiveSession.mockClear();
  queries.archiveSession.mockImplementationOnce(() => ({ unwrap: () => Promise.reject(new Error("failed")) }));
  queries.workspaces = [workspace("workspace", "项目")];
  queries.sessions = [{
    session_id: "session", workspace_id: "workspace", title: "会话",
    updated_at: null, activity: "idle", has_active_subagents: false, needs_attention: false,
  }];
  renderSidebar("/sessions/session");

  fireEvent.click(screen.getByLabelText("项目 工作区操作"));
  fireEvent.click(await screen.findByRole("menuitem", { name: "归档所有会话" }));
  expect(await screen.findByText("归档失败")).toBeInTheDocument();
  expect(screen.getByTestId("current-path")).toHaveTextContent("/sessions/session");
});

it("待处理按钮显示收件箱条数并进入收件箱", () => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  queries.inbox = [{ kind: "authorization", label: "验证熊猫" }, { kind: "authorization", label: "验证熊猫" }];
  renderSidebar("/");

  fireEvent.click(screen.getByRole("button", { name: "待处理 · 2" }));
  expect(screen.getByTestId("current-path")).toHaveTextContent("/inbox");
});
