import { MantineProvider } from "@mantine/core";
import { configureStore } from "@reduxjs/toolkit";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Provider } from "react-redux";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { kondenApi, useGetAvatarsQuery } from "../src/api/kondenApi";
import { BotAvatarPicker } from "../src/features/conversation/BotAvatarPicker";
import { BotAvatars } from "../src/features/conversation/ChatPresentation";
import runtimeReducer from "../src/realtime/runtimeSlice";

const multipartBodies = new WeakMap<Request, FormData>();
const stores: ReturnType<typeof createStore>[] = [];
function createStore() {
  return configureStore({
    reducer: { runtime: runtimeReducer, [kondenApi.reducerPath]: kondenApi.reducer },
    middleware: (defaults) => defaults().concat(kondenApi.middleware),
  });
}
beforeEach(() => {
  vi.stubGlobal("crypto", { getRandomValues: crypto.getRandomValues.bind(crypto) });
  const NativeRequest = Request;
  vi.stubGlobal("Request", class extends NativeRequest {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      super(typeof input === "string" ? new URL(input, "http://localhost") : input, init);
      // Read jsdom FormData before Node's Request serializes it.
      if (init?.body instanceof FormData) multipartBodies.set(this, init.body);
    }
  });
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
});
afterEach(() => {
  cleanup();
  for (const store of stores.splice(0)) store.dispatch(kondenApi.util.resetApiState());
  vi.unstubAllGlobals();
});
function showPicker() {
  const store = createStore(); stores.push(store);
  function Picker() {
    const { data: avatars = {} } = useGetAvatarsQuery();
    return <BotAvatars value={avatars}><BotAvatarPicker name="Ada" sessionId="ada" connectionId="connection" /></BotAvatars>;
  }
  const view = render(<Provider store={store}><MantineProvider env="test"><Picker /></MantineProvider></Provider>);
  return { ...view, store };
}

it("保存失败保留原头像与候选，显示原因，重试成功才更新并关闭", async () => {
  let resolve!: (value: Response) => void;
  let body: { connection_id: string; seed: string } | undefined;
  let selected = { kind: "generated" as const, seed: "original-seed" };
  let attempt = 0;
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => {
    if (request.method === "GET") return Response.json({ ada: selected });
    body = await request.json();
    if (++attempt === 1) return new Promise<Response>((done) => { resolve = done; });
    selected = { kind: "generated", seed: body!.seed };
    return Response.json(selected);
  }));
  const { store } = showPicker();
  await waitFor(() => expect(kondenApi.endpoints.getAvatars.select()(store.getState()).data?.ada).toEqual(selected));
  const trigger = screen.getByRole("button", { name: "修改 Ada 的头像" });
  const original = trigger.querySelector("path")!.getAttribute("d");
  fireEvent.click(trigger);
  const firstBatch = screen.getByRole("button", { name: "使用头像 1" }).querySelector("path")!.getAttribute("d");
  fireEvent.click(screen.getByRole("button", { name: "换一批" }));
  const candidate = screen.getByRole("button", { name: "使用头像 1" });
  expect(candidate.querySelector("path")!.getAttribute("d")).not.toBe(firstBatch);
  const candidatePath = candidate.querySelector("path")!.getAttribute("d");
  fireEvent.click(candidate);
  await waitFor(() => expect(candidate).toBeDisabled());
  expect(screen.getByRole("button", { name: "换一批" })).toBeDisabled();
  await waitFor(() => expect(resolve).toBeTypeOf("function"));
  resolve(Response.json({ detail: "头像暂时无法保存，请重试" }, { status: 400 }));
  expect(await screen.findByRole("alert")).toHaveTextContent("头像暂时无法保存，请重试");
  expect(trigger.querySelector("path")).toHaveAttribute("d", original);
  expect(candidate.querySelector("path")).toHaveAttribute("d", candidatePath);
  fireEvent.click(candidate);
  await waitFor(() => expect(screen.queryByRole("button", { name: "使用头像 1" })).not.toBeInTheDocument());
  expect(body).toEqual({ connection_id: "connection", seed: selected.seed });
  expect(trigger.querySelector("path")).toHaveAttribute("d", candidatePath);
});

it("上传头像使用独立图片接口，恢复默认删除界面覆盖项", async () => {
  const attachmentId = `sha256:${"a".repeat(64)}`;
  const upload = vi.fn();
  let selection: { kind: "image"; attachment_id: string } | null = null;
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => {
    if (request.method === "GET") return Response.json(selection === null ? {} : { ada: selection });
    if (request.method === "POST") {
      expect(new URL(request.url).pathname).toBe("/api/agents/ada/avatar/image");
      const data = multipartBodies.get(request)!;
      upload(data.get("connection_id"), (data.get("file") as File).name);
      selection = { kind: "image", attachment_id: attachmentId };
      return Response.json(selection);
    }
    expect(await request.json()).toEqual({ connection_id: "connection", seed: null });
    selection = null;
    return Response.json(null);
  }));
  const { store } = showPicker();
  await waitFor(() => expect(kondenApi.endpoints.getAvatars.select()(store.getState()).isSuccess).toBe(true));
  const trigger = screen.getByRole("button", { name: "修改 Ada 的头像" });
  fireEvent.click(trigger);
  expect(screen.getByRole("button", { name: "恢复默认" })).toBeDisabled();
  const fileInput = document.querySelector<HTMLInputElement>('input[type="file"]')!;
  fireEvent.change(fileInput, { target: { files: [new File(["avatar"], "custom.png", { type: "image/png" })] } });
  await waitFor(() => expect(trigger.querySelector("img")).toHaveAttribute("src", `/api/avatars/images/${encodeURIComponent(attachmentId)}`));
  expect(upload).toHaveBeenCalledWith("connection", "custom.png");
  fireEvent.click(trigger);
  fireEvent.click(screen.getByRole("button", { name: "恢复默认" }));
  await waitFor(() => expect(trigger.querySelector("img")).toBeNull());
  expect(trigger.querySelector("path")).not.toBeNull();
  expect(kondenApi.endpoints.getAvatars.select()(store.getState()).data).toEqual({});
});
