import { MantineProvider } from "@mantine/core";
import { configureStore } from "@reduxjs/toolkit";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { Provider } from "react-redux";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { SubagentObservation } from "../src/api/contracts";
import { SubagentPanel } from "../src/features/conversation/SubagentPanel";
import { SubagentCallCard } from "../src/features/conversation/SubagentCallCard";
import runtimeReducer, { bindOwner, connected, previewDelta, previewStarted, sessionActivity, thinkingDelta, thinkingStarted, toolProgress, viewing } from "../src/realtime/runtimeSlice";

const query = vi.hoisted(() => ({ data: undefined as SubagentObservation | undefined, read: vi.fn() }));
vi.mock("../src/api/helpermeApi", () => ({
  useObserveSubagentQuery: (args: unknown) => { query.read(args); return { currentData: query.data, isError: false }; },
}));
vi.mock("../src/features/conversation/MarkdownMessage", () => ({
  MarkdownMessage: ({ content }: { content: string }) => <div>{content}</div>,
}));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
});

it("观察子过程、切换和关闭面板都保持父 owner，并保留交回历史", () => {
  const store = configureStore({ reducer: { runtime: runtimeReducer } });
  store.dispatch(connected("connection"));
  store.dispatch(bindOwner("parent"));
  store.dispatch(viewing("parent"));
  query.data = {
    session_id: "child", task: "调查工具", activity: "running", result: null,
    conversation: {
      session_id: "child", workspace_id: "workspace", revision: 1, items: [{
        kind: "step", step_id: "step", output_id: "old", text: "已提交过程", thinking: "历史思考",
        tools: [{ command_id: "cmd", name: "read_file", status: "queued", error: null, arguments: { path: "a.py" } }],
        occurred_at: "2026-10-03T00:00:00Z", rewindable: true,
      }],
      session: { status: "waiting", waiting_for: ["external_fact"], pending_authorization_ids: [],
        pending_authorization_commands: [], should_wake: false, has_active_subagents: false,
        control_approval: null, control_message: null, auto_authorize: false, paused: false },
      compact_count: 0, compact_phase: null, context_input_tokens: null, waiting_until: null,
      work_plan: null, work_plan_updates: [], workspace_version: null,
    },
  };
  store.dispatch(sessionActivity({ sessionId: "child", activity: "running" }));
  store.dispatch(toolProgress({ sessionId: "child", commandId: "cmd", name: "read_file", status: "running" }));
  store.dispatch(previewStarted({ sessionId: "child", outputId: "live" }));
  store.dispatch(previewDelta({ sessionId: "child", outputId: "live", text: "正在输出" }));
  store.dispatch(thinkingStarted({ sessionId: "child", outputId: "live" }));
  store.dispatch(thinkingDelta({ sessionId: "child", outputId: "live", text: "实时思考" }));
  const onClose = vi.fn();
  const show = (commandId: string) => <Provider store={store}><MantineProvider>
    <SubagentPanel parentSessionId="parent" commandId={commandId} onClose={onClose} />
  </MantineProvider></Provider>;
  const view = render(show("delegate"));
  expect(query.read).toHaveBeenLastCalledWith({ parentSessionId: "parent", commandId: "delegate" });
  expect(screen.getByRole("button", { name: /1.*已提交过程/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /2.*正在输出/ })).toBeInTheDocument();
  expect(screen.getByText("实时思考")).toBeInTheDocument();
  expect(screen.queryByLabelText("从这一步之后重开")).toBeNull();
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(screen.queryByRole("button", { name: "允许" })).toBeNull();
  act(() => {
    store.dispatch(sessionActivity({ sessionId: "child", activity: "idle" }));
    query.data = { ...query.data!, activity: "idle", result: {
      reported: true, cancelled: false, summary: "调查结论", failure: null, reason: null,
    }};
  });
  view.rerender(show("delegate"));
  expect(screen.getByText("已交回")).toBeInTheDocument();
  expect(screen.getByText("调查结论")).toBeInTheDocument();
  query.data = { ...query.data!, session_id: "other-child", task: "另一个任务", conversation: null, result: null };
  view.rerender(show("other-delegate"));
  expect(screen.getByText("另一个任务")).toBeInTheDocument();
  expect(screen.queryByText("正在输出")).toBeNull();
  fireEvent.click(screen.getByLabelText("关闭子 Agent 面板"));
  expect(onClose).toHaveBeenCalledOnce();
  expect(store.getState().runtime.ownerSessionId).toBe("parent");
  expect(store.getState().runtime.viewingSessionId).toBe("parent");
});

it("派出回执提供观察入口，使用已提交的 Command 身份", () => {
  const onObserve = vi.fn();
  render(<MantineProvider><SubagentCallCard tool={{
    commandId: "delegate", name: "delegate", status: "succeeded", error: null, arguments: { task: "调查工具" },
  }} onObserve={onObserve} /></MantineProvider>);
  expect(screen.getByText("已派出")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "查看过程" }));
  expect(onObserve).toHaveBeenCalledWith("delegate");
});
