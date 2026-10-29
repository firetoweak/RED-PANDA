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

it("确认命名后创建，可复制及重新查看配置，关闭后隐藏凭据且支持撤销", async () => {
  const store = createStore(); stores.push(store);
  let item: ExternalConnection = { connection_id: "a".repeat(32), name: "笔记本 Codex" };
  let entries: ExternalConnection[] = [];
  const writeText = vi.fn().mockRejectedValueOnce(new Error("clipboard denied")).mockResolvedValue(undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText }, userAgent: navigator.userAgent });
  const removed = vi.fn();
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => {
    expect(new URL(request.url).pathname).toMatch(/^\/api\/external-connections/);
    if (request.method === "GET") return Response.json(entries);
    if (new URL(request.url).pathname.endsWith("/configuration")) {
      expect(await request.json()).toEqual({ connection_id: "human" });
      return Response.json({ connection: item, token: "one-time-secret", url: "http://localhost/mcp/" });
    }
    if (request.method === "POST") {
      expect(await request.json()).toEqual({ connection_id: "human", name: "笔记本 Codex" });
      entries = [item];
      return Response.json({ connection: item, token: "one-time-secret", url: "http://localhost/mcp/" }, { status: 201 });
    }
    if (request.method === "PUT") {
      expect(await request.json()).toEqual({ connection_id: "human", name: "台式机 Codex" });
      item = { ...item, name: "台式机 Codex" }; entries = [item];
      return Response.json(item);
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
  fireEvent.click(screen.getByRole("button", { name: "新建连接配置" }));
  expect(entries).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "取消创建" }));
  expect(entries).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "新建连接配置" }));
  expect(screen.getByRole("button", { name: "确认生成" })).toBeDisabled();
  fireEvent.change(screen.getByRole("textbox", { name: /接入名称/ }), { target: { value: "笔记本 Codex" } });
  fireEvent.click(screen.getByRole("button", { name: "确认生成" }));
  expect(await screen.findByRole("textbox", { name: "URL" })).toHaveValue("http://localhost/mcp/");
  expect(screen.getByLabelText("HTTP 请求头：Authorization")).toHaveValue("Bearer one-time-secret");
  expect(screen.queryByRole("textbox", { name: "Codex MCP 配置（TOML）" })).not.toBeInTheDocument();
  await waitFor(() => expect(JSON.stringify(store.getState())).not.toContain("one-time-secret"));
  fireEvent.click(screen.getByRole("button", { name: "复制 URL" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("复制失败，请选中对应字段手动复制。");
  fireEvent.click(screen.getByRole("button", { name: "复制 URL" }));
  expect(await screen.findByRole("button", { name: "已复制" })).toBeVisible();
  expect(writeText).toHaveBeenLastCalledWith("http://localhost/mcp/");
  fireEvent.click(screen.getByRole("button", { name: "复制请求头值" }));
  await waitFor(() => expect(writeText).toHaveBeenLastCalledWith("Bearer one-time-secret"));
  expect(screen.getByRole("button", { name: "复制 URL" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "高级：手动配置 TOML" }));
  const config = await screen.findByRole("textbox", { name: "Codex MCP 配置（TOML）" });
  const value = (config as HTMLTextAreaElement).value;
  expect(value).toBe('[mcp_servers.konden]\nurl = "http://localhost/mcp/"\n\n[mcp_servers.konden.http_headers]\nAuthorization = "Bearer one-time-secret"');
  fireEvent.click(screen.getByRole("button", { name: "复制 TOML" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "已复制" })).toBeVisible());
  expect(writeText).toHaveBeenLastCalledWith(value);
  view.unmount();
  render(<Provider store={store}><MantineProvider env="test"><ExternalAccess /></MantineProvider></Provider>);
  expect(screen.queryByLabelText("URL")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("HTTP 请求头：Authorization")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("Codex MCP 配置（TOML）")).not.toBeInTheDocument();
  fireEvent.click(await screen.findByRole("button", { name: "查看配置 笔记本 Codex" }));
  expect(await screen.findByLabelText("HTTP 请求头：Authorization")).toHaveValue("Bearer one-time-secret");
  await waitFor(() => expect(JSON.stringify(store.getState())).not.toContain("one-time-secret"));
  fireEvent.click(screen.getByRole("button", { name: "返回连接列表" }));
  fireEvent.click(screen.getByRole("button", { name: "重命名 笔记本 Codex" }));
  fireEvent.change(screen.getByLabelText("修改接入名称"), { target: { value: "台式机 Codex" } });
  fireEvent.click(screen.getByRole("button", { name: "保存名称" }));
  fireEvent.click(await screen.findByRole("button", { name: "撤销 台式机 Codex" }));
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(removed).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "撤销 台式机 Codex" }));
  fireEvent.click(screen.getByRole("button", { name: "确认撤销" }));
  await waitFor(() => expect(screen.queryByRole("button", { name: "撤销 台式机 Codex" })).not.toBeInTheDocument());
  expect(removed).toHaveBeenCalledOnce();
});
