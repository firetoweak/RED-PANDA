import { AppShell, MantineProvider, Text } from "@mantine/core";
import { configureStore } from "@reduxjs/toolkit";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Provider } from "react-redux";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import type { SessionSummary, Workspace } from "../src/api/contracts";
import { SessionSidebar } from "../src/features/sessions/SessionSidebar";
import runtimeReducer, { sessionActivity } from "../src/realtime/runtimeSlice";

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
  workspaces: [] as Workspace[],
  deleteWorkspace: vi.fn(() => ({ unwrap: () => Promise.resolve() })),
}));
vi.mock("../src/api/redpandaApi", () => ({
  useGetSessionsQuery: () => ({ data: queries.sessions, isLoading: false }),
  useGetSessionTitlesQuery: () => ({ data: {} }),
  useGetWorkspacesQuery: () => ({ data: queries.workspaces, isLoading: false }),
  useArchiveSessionMutation: () => [vi.fn()],
  useSetSessionTitleMutation: () => [vi.fn()],
  useCreateWorkspaceMutation: () => [vi.fn(), {}],
  useSelectWorkspaceDirectoryMutation: () => [vi.fn(), {}],
  useDeleteWorkspaceMutation: () => [
    queries.deleteWorkspace,
    { isLoading: false, isError: false, reset: vi.fn() },
  ],
}));

function CurrentPath() {
  return <Text data-testid="current-path">{useLocation().pathname}</Text>;
}

function renderSidebar(initialEntry: string) {
  const store = configureStore({ reducer: { runtime: runtimeReducer } });
  return render(
    <Provider store={store}>
      <MantineProvider>
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
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("父会话空闲时仍显示子任务旋钮，回收后恢复空闲标记", () => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  queries.workspaces = [workspace("workspace", "工作区")];
  queries.sessions = [{
    session_id: "parent", workspace_id: "workspace", title: "父会话",
    updated_at: null, activity: "idle", has_active_subagents: true,
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
});

it("删除工作区要先确认：取消不碰接口，确认才带上工作区 id", async () => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  queries.deleteWorkspace.mockClear();
  queries.workspaces = [workspace("workspace", "工作区")];
  queries.sessions = [];
  renderSidebar("/");

  fireEvent.click(screen.getByLabelText("删除 工作区"));
  expect(screen.getByText("删除工作区")).toBeInTheDocument();
  expect(queries.deleteWorkspace).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  await waitFor(() =>
    expect(screen.queryByRole("button", { name: "删除" })).toBeNull(),
  );
  expect(queries.deleteWorkspace).not.toHaveBeenCalled();

  fireEvent.click(screen.getByLabelText("删除 工作区"));
  fireEvent.click(screen.getByRole("button", { name: "删除" }));
  await waitFor(() =>
    expect(queries.deleteWorkspace).toHaveBeenCalledWith({
      workspaceId: "workspace",
    }),
  );
  await waitFor(() =>
    expect(screen.queryByRole("button", { name: "删除" })).toBeNull(),
  );
});

it("删掉正在聊天的工作区后，离开到剩下的工作区", async () => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  queries.deleteWorkspace.mockClear();
  queries.workspaces = [workspace("workspace-a", "旧项目"), workspace("workspace-b", "新项目")];
  queries.sessions = [{
    session_id: "session-1", workspace_id: "workspace-a", title: "旧会话",
    updated_at: null, activity: "idle", has_active_subagents: false,
  }];
  renderSidebar("/sessions/session-1");
  expect(screen.getByTestId("current-path")).toHaveTextContent("/sessions/session-1");

  fireEvent.click(screen.getByLabelText("删除 旧项目"));
  fireEvent.click(screen.getByRole("button", { name: "删除" }));

  await waitFor(() =>
    expect(queries.deleteWorkspace).toHaveBeenCalledWith({
      workspaceId: "workspace-a",
    }),
  );
  await waitFor(() =>
    expect(screen.getByTestId("current-path")).toHaveTextContent("/workspaces/workspace-b"),
  );
});
