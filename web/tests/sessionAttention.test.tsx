import { MantineProvider } from "@mantine/core";
import { configureStore } from "@reduxjs/toolkit";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Provider } from "react-redux";
import { afterEach, expect, it, vi } from "vitest";

import type { InboxItem } from "../src/api/contracts";
import { redpandaApi } from "../src/api/redpandaApi";
import { SessionAttention } from "../src/features/conversation/SessionAttention";
import runtimeReducer from "../src/realtime/runtimeSlice";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("对话只处理所属 Bot 的通知，例行任务决定沿用原接口并更新提示", async () => {
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  const NativeRequest = Request;
  vi.stubGlobal("Request", class extends NativeRequest {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      super(typeof input === "string" ? new URL(input, "http://localhost") : input, init);
    }
  });
  const common = { session_id: "ada", workspace_id: null, label: "Ada" };
  let inbox: InboxItem[] = [
    { ...common, kind: "absence", phase: "asked", summary: "要让 Ada 的例行任务继续吗？" },
    { ...common, kind: "limit", event_id: "limit", recipient: "Lin", text: "请调整交流方向。" },
    { ...common, kind: "authorization", command_id: "write", name: "write_file", arguments: {} },
    { ...common, kind: "stopped", session_id: "lin", label: "Lin", summary: "Lin 执行已停止" },
  ];
  const changes: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => {
    if (request.method === "POST") {
      changes.push([new URL(request.url).pathname, await request.json()]);
      inbox = inbox.filter((item) => item.kind !== "absence");
      return Response.json({});
    }
    return Response.json(inbox);
  }));
  const store = configureStore({
    reducer: { runtime: runtimeReducer, [redpandaApi.reducerPath]: redpandaApi.reducer },
    middleware: (getDefaultMiddleware) => getDefaultMiddleware().concat(redpandaApi.middleware),
  });
  const view = render(<Provider store={store}><MantineProvider env="test">
    <SessionAttention sessionId="ada" connectionId={null} />
  </MantineProvider></Provider>);
  expect(await screen.findByText("要让 Ada 的例行任务继续吗？")).toBeInTheDocument();
  expect(screen.getByText(/请调整交流方向/)).toBeInTheDocument();
  expect(screen.queryByText("Lin 执行已停止")).not.toBeInTheDocument();
  expect(screen.queryByText("write_file")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "继续" })).toBeDisabled();
  view.rerender(<Provider store={store}><MantineProvider env="test">
    <SessionAttention sessionId="ada" connectionId="connection" />
  </MantineProvider></Provider>);
  fireEvent.click(screen.getByRole("button", { name: "停下" }));
  await waitFor(() => expect(changes).toEqual([
    ["/api/inbox/absence", { connection_id: "connection", session_id: "ada", action: "stop" }],
  ]));
  await waitFor(() => expect(screen.queryByText("要让 Ada 的例行任务继续吗？")).not.toBeInTheDocument());
  expect(screen.getByText(/请调整交流方向/)).toBeInTheDocument();
  cleanup();
  store.dispatch(redpandaApi.util.resetApiState());
});
