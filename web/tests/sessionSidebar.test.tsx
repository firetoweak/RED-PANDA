import { AppShell, MantineProvider } from "@mantine/core";
import { configureStore } from "@reduxjs/toolkit";
import { act, cleanup, render, screen } from "@testing-library/react";
import { Provider } from "react-redux";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import type { SessionSummary } from "../src/api/contracts";
import { SessionSidebar } from "../src/features/sessions/SessionSidebar";
import runtimeReducer, { sessionActivity } from "../src/realtime/runtimeSlice";

const queries = vi.hoisted(() => ({ sessions: [] as SessionSummary[] }));
vi.mock("../src/api/redpandaApi", () => ({
  useGetSessionsQuery: () => ({ data: queries.sessions, isLoading: false }),
  useGetSessionTitlesQuery: () => ({ data: {} }),
  useGetWorkspacesQuery: () => ({ data: [{ workspace_id: "workspace", name: "工作区" }], isLoading: false }),
  useArchiveSessionMutation: () => [vi.fn()],
  useSetSessionTitleMutation: () => [vi.fn()],
  useCreateWorkspaceMutation: () => [vi.fn(), {}],
  useSelectWorkspaceDirectoryMutation: () => [vi.fn(), {}],
}));

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("父会话空闲时仍显示子任务旋钮，回收后恢复空闲标记", () => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
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
