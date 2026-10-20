import { MantineProvider } from "@mantine/core";
import { configureStore } from "@reduxjs/toolkit";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Provider } from "react-redux";
import { afterEach, expect, it, vi } from "vitest";

import type { Routine } from "../src/api/contracts";
import { redpandaApi } from "../src/api/redpandaApi";
import { RoutineList } from "../src/features/conversation/RoutineList";
import runtimeReducer from "../src/realtime/runtimeSlice";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("暂停、恢复与二次确认后的取消都按例行任务调用设置状态的接口", async () => {
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  const NativeRequest = Request;
  vi.stubGlobal("Request", class extends NativeRequest {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      super(typeof input === "string" ? new URL(input, "http://localhost") : input, init);
    }
  });
  const routines: Routine[] = [
    { routine_id: "r-pr", name: "检查 PR", intent: "看评审", schedule: "@every 10m", state: "active",
      next_run: "2026-10-19T02:00:00+00:00", last_run: null },
    { routine_id: "r-build", name: "夜间构建", intent: "跑构建", schedule: "0 2 * * *", state: "paused_by_user",
      next_run: null, last_run: null },
  ];
  const changes: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => {
    if (request.method === "PUT") {
      changes.push([new URL(request.url).pathname, await request.json()]);
    }
    return Response.json(routines);
  }));
  const store = configureStore({
    reducer: { runtime: runtimeReducer, [redpandaApi.reducerPath]: redpandaApi.reducer },
    middleware: (getDefaultMiddleware) => getDefaultMiddleware().concat(redpandaApi.middleware),
  });
  render(
    <Provider store={store}><MantineProvider env="test">
      <RoutineList sessionId="agent" connectionId="connection" />
    </MantineProvider></Provider>,
  );
  expect(await screen.findByText("人暂停")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "恢复 检查 PR" })).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "暂停 检查 PR" }));
  fireEvent.click(screen.getByRole("button", { name: "恢复 夜间构建" }));
  await waitFor(() => expect(changes).toHaveLength(2));
  await waitFor(() => expect(screen.getByRole("button", { name: "取消 检查 PR" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "取消 检查 PR" }));
  expect(changes).toHaveLength(2);
  fireEvent.click(screen.getByRole("button", { name: "确认取消 检查 PR" }));
  await waitFor(() => expect(changes).toEqual([
    ["/api/sessions/agent/routines/r-pr/state", { connection_id: "connection", state: "paused_by_user" }],
    ["/api/sessions/agent/routines/r-build/state", { connection_id: "connection", state: "active" }],
    ["/api/sessions/agent/routines/r-pr/state", { connection_id: "connection", state: "deleted" }],
  ]));
  cleanup();
  store.dispatch(redpandaApi.util.resetApiState());
});
