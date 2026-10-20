import { MantineProvider } from "@mantine/core";
import { configureStore } from "@reduxjs/toolkit";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Provider } from "react-redux";
import { afterEach, expect, it, vi } from "vitest";

import type { Routine } from "../src/api/contracts";
import { redpandaApi } from "../src/api/redpandaApi";
import { RoutineList } from "../src/features/conversation/RoutineList";
import { registerTimelineFocus } from "../src/features/conversation/timelineFocus";
import runtimeReducer from "../src/realtime/runtimeSlice";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const routines: Routine[] = [
  { routine_id: "r-pr", name: "检查 PR", intent: "看评审", schedule: "@every 10m", state: "active",
    next_run: "2026-10-20T11:10:00+00:00", last_run: "2026-10-20T11:00:00+00:00" },
  { routine_id: "r-build", name: "夜间构建", intent: "跑构建", schedule: "0 2 * * *", state: "paused_by_user",
    next_run: null, last_run: null },
  { routine_id: "r-watch", name: "巡检", intent: "看服务", schedule: "*/10 9-18 * * 1-5", state: "paused",
    next_run: null, last_run: null },
];

const fires = [
  { fire_id: "f-new", planned_at: "2026-10-20T11:00:00+00:00", fired_at: "2026-10-20T11:00:02+00:00",
    delivered_at: "2026-10-20T11:00:03+00:00", output_id: "event-2" },
  { fire_id: "f-old", planned_at: "2026-10-20T10:00:00+00:00", fired_at: "2026-10-20T10:00:02+00:00",
    delivered_at: null, output_id: null },
];

const conversation = {
  session_id: "agent", workspace_id: null, revision: 1,
  items: [{
    kind: "step", step_id: "step-1", output_id: "event-2", text: "没有新评审。",
    thinking: null, tools: [], occurred_at: "2026-10-20T11:00:20+00:00", files: [],
  }],
  session: {
    status: "waiting", waiting_for: ["external_fact"], pending_authorization_ids: [],
    pending_authorization_commands: [], should_wake: false, has_active_subagents: false,
    control_approval: null, control_message: null, auto_authorize: false, paused: false, last_error: null,
  },
  compact_count: 0, compact_phase: null, waiting_until: null, work_plan: null,
  work_plan_updates: [], context_input_tokens: null,
};

function renderList(onOpenTimeline?: () => void) {
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  const NativeRequest = Request;
  vi.stubGlobal("Request", class extends NativeRequest {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      super(typeof input === "string" ? new URL(input, "http://localhost") : input, init);
    }
  });
  const changes: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => {
    const url = new URL(request.url);
    if (request.method === "PUT") {
      changes.push([url.pathname, await request.json()]);
      return Response.json(routines);
    }
    if (url.pathname.endsWith("/fires")) return Response.json(fires);
    if (url.pathname.endsWith("/routines")) return Response.json(routines);
    return Response.json(conversation);
  }));
  const store = configureStore({
    reducer: { runtime: runtimeReducer, [redpandaApi.reducerPath]: redpandaApi.reducer },
    middleware: (getDefaultMiddleware) => getDefaultMiddleware().concat(redpandaApi.middleware),
  });
  render(
    <Provider store={store}><MantineProvider env="test">
      <RoutineList sessionId="agent" connectionId="connection" onOpenTimeline={onOpenTimeline} />
    </MantineProvider></Provider>,
  );
  return { changes, store };
}

it("列表用可读安排显示上次和下次，暂停、恢复与二次确认后的删除仍调用设置状态的接口", async () => {
  const { changes, store } = renderList();
  expect(await screen.findByText("人暂停")).toBeInTheDocument();
  expect(screen.getByText("每 10 分钟")).toBeInTheDocument();
  expect(screen.getByText("每天 02:00")).toBeInTheDocument();
  expect(screen.getByText("*/10 9-18 * * 1-5")).toBeInTheDocument();
  expect(screen.queryByText("@every 10m")).toBeNull();
  expect(screen.queryByText("0 2 * * *")).toBeNull();
  expect(screen.getAllByText(/上次 .+ · 下次 /)).toHaveLength(3);
  expect(screen.getAllByText(/尚未运行/).length).toBeGreaterThan(0);
  expect(screen.getAllByText(/未安排/).length).toBeGreaterThan(0);
  expect(screen.queryByRole("switch", { name: "恢复 检查 PR" })).toBeNull();

  fireEvent.click(screen.getByRole("switch", { name: "暂停 检查 PR" }));
  fireEvent.click(screen.getByRole("switch", { name: "恢复 夜间构建" }));
  await waitFor(() => expect(changes).toHaveLength(2));
  await waitFor(() => expect(screen.getByRole("button", { name: "删除 检查 PR" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "删除 检查 PR" }));
  expect(changes).toHaveLength(2);
  fireEvent.click(screen.getByRole("button", { name: "确认删除 检查 PR" }));
  await waitFor(() => expect(changes).toEqual([
    ["/api/sessions/agent/routines/r-pr/state", { connection_id: "connection", state: "paused_by_user" }],
    ["/api/sessions/agent/routines/r-build/state", { connection_id: "connection", state: "active" }],
    ["/api/sessions/agent/routines/r-pr/state", { connection_id: "connection", state: "deleted" }],
  ]));
  cleanup();
  store.dispatch(redpandaApi.util.resetApiState());
});

it("点开例行任务能看到完整意图和倒序的执行记录，已出现在时间线上的一次可以跳过去", async () => {
  const jumped: string[] = [];
  const unregister = registerTimelineFocus((outputId) => { jumped.push(outputId); });
  const { store } = renderList(() => { jumped.push("open-timeline"); });
  fireEvent.click(await screen.findByRole("button", { name: "查看 检查 PR" }));
  expect(await screen.findByText("看评审")).toBeInTheDocument();
  expect(screen.getByText("每 10 分钟")).toBeInTheDocument();
  expect(screen.getByText("@every 10m")).toBeInTheDocument();
  const delivered = await screen.findByText("已送达");
  const pending = screen.getByText("未送达");
  expect(delivered.compareDocumentPosition(pending) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  const jump = await screen.findByRole("button", { name: /在时间线中查看/ });
  expect(screen.getAllByRole("button", { name: /在时间线中查看/ })).toHaveLength(1);
  fireEvent.click(jump);
  expect(jumped).toEqual(["open-timeline", "event-2"]);
  fireEvent.click(screen.getByRole("button", { name: "返回例行任务" }));
  expect(screen.getByText("每 10 分钟")).toBeInTheDocument();
  unregister();
  cleanup();
  store.dispatch(redpandaApi.util.resetApiState());
});
