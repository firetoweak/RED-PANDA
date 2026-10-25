import { MantineProvider } from "@mantine/core";
import { configureStore } from "@reduxjs/toolkit";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Provider } from "react-redux";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { kondenApi } from "../src/api/kondenApi";
import type { ExternalConnection } from "../src/api/contracts";
import { ExternalAccess } from "../src/features/settings/ExternalAccess";
import runtimeReducer from "../src/realtime/runtimeSlice";

const stores: ReturnType<typeof createStore>[] = [];
function createStore() {
  return configureStore({
    reducer: { runtime: runtimeReducer, [kondenApi.reducerPath]: kondenApi.reducer },
    preloadedState: { runtime: { ...runtimeReducer(undefined, { type: "init" }), connectionId: "human" } },
    middleware: (defaults) => defaults().concat(kondenApi.middleware),
  });
}
beforeEach(() => {
  const NativeRequest = Request;
  vi.stubGlobal("Request", class extends NativeRequest {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      super(typeof input === "string" ? new URL(input, "http://localhost") : input, init);
    }
  });
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  Object.defineProperty(document, "fonts", { configurable: true, value: new EventTarget() });
});
afterEach(() => {
  cleanup();
  for (const store of stores.splice(0)) store.dispatch(kondenApi.util.resetApiState());
  vi.unstubAllGlobals();
  Reflect.deleteProperty(document, "fonts");
});

it("页面可创建、复制和撤销，关闭后不再显示凭据，凭据不留在查询或 mutation 中", async () => {
  const store = createStore(); stores.push(store);
  const item = { connection_id: "a".repeat(32) };
  let entries: ExternalConnection[] = [];
  const writeText = vi.fn().mockRejectedValueOnce(new Error("clipboard denied")).mockResolvedValue(undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText }, userAgent: navigator.userAgent });
  const removed = vi.fn();
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => {
    expect(new URL(request.url).pathname).toMatch(/^\/api\/external-connections/);
    if (request.method === "GET") return Response.json(entries);
    if (request.method === "POST") {
      expect(await request.json()).toEqual({ connection_id: "human" });
      entries = [item];
      return Response.json({ connection: item, token: "one-time-secret", url: "http://localhost/mcp/" }, { status: 201 });
    }
    expect(await request.json()).toEqual({ connection_id: "human" });
    expect(new URL(request.url).pathname.endsWith(item.connection_id)).toBe(true);
    removed(); entries = [];
    return new Response(null, { status: 204 });
  }));
  const view = render(<Provider store={store}><MantineProvider env="test">
    <ExternalAccess />
  </MantineProvider></Provider>);
  expect(await screen.findByText("尚未生成连接配置。")).toBeVisible();
  expect(screen.queryByLabelText("接入名称")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "生成连接配置" }));
  const config = await screen.findByLabelText("MCP 配置（JSON）");
  const value = (config as HTMLTextAreaElement).value;
  expect(JSON.parse(value).mcpServers.konden).toEqual({
    url: "http://localhost/mcp/", headers: { Authorization: "Bearer one-time-secret" },
  });
  await waitFor(() => expect(JSON.stringify(store.getState())).not.toContain("one-time-secret"));
  fireEvent.click(screen.getByRole("button", { name: "复制配置" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("复制失败，请选中上方配置手动复制。");
  fireEvent.click(screen.getByRole("button", { name: "复制配置" }));
  expect(await screen.findByRole("button", { name: "已复制" })).toBeVisible();
  expect(writeText).toHaveBeenLastCalledWith(value);
  view.unmount();
  render(<Provider store={store}><MantineProvider env="test"><ExternalAccess /></MantineProvider></Provider>);
  expect(screen.queryByLabelText("MCP 配置（JSON）")).not.toBeInTheDocument();
  fireEvent.click(await screen.findByRole("button", { name: "撤销 连接 aaaaaaaa" }));
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(removed).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "撤销 连接 aaaaaaaa" }));
  fireEvent.click(screen.getByRole("button", { name: "确认撤销" }));
  await waitFor(() => expect(screen.queryByRole("button", { name: "撤销 连接 aaaaaaaa" })).not.toBeInTheDocument());
  expect(removed).toHaveBeenCalledOnce();
});
