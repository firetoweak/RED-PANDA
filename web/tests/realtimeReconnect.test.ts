import { configureStore } from "@reduxjs/toolkit";
import { afterEach, expect, it, vi } from "vitest";
import { helpermeApi } from "../src/api/helpermeApi";
import { openEventBridge } from "../src/realtime/eventBridge";
import runtimeReducer from "../src/realtime/runtimeSlice";

afterEach(() => vi.unstubAllGlobals());

it("断线清掉旧流，重连快照与后续增量在相同输出身份下衔接", () => {
  const listeners = new Map<string, (event: MessageEvent) => void>();
  vi.stubGlobal("EventSource", class {
    addEventListener(name: string, listener: (event: MessageEvent) => void) { listeners.set(name, listener); }
    close() {}
  });
  const store = configureStore({
    reducer: { runtime: runtimeReducer, [helpermeApi.reducerPath]: helpermeApi.reducer },
    middleware: (getDefaultMiddleware) => getDefaultMiddleware().concat(helpermeApi.middleware),
  });
  const close = openEventBridge(store.dispatch);
  const emit = (name: string, payload: object = {}) => listeners.get(name)!(new MessageEvent(name, { data: JSON.stringify(payload) }));
  const id = { session_id: "child", output_id: "output" };
  emit("connected", { connection_id: "first" });
  emit("session_activity", { session_id: "child", activity: "running" });
  emit("preview.started", id);
  emit("preview.delta", { ...id, text: "旧片段" });
  emit("thinking.started", id);
  emit("thinking.delta", { ...id, text: "旧思考" });
  emit("error");
  expect(store.getState().runtime.sessions.child.activePreview).toBeNull();
  expect(store.getState().runtime.sessions.child.activeThinking).toBeNull();
  expect(store.getState().runtime.sessions.child.activity).toBeNull();
  emit("connected", { connection_id: "second" });
  emit("session_activity", { session_id: "child", activity: "running" });
  emit("preview.started", id);
  emit("preview.delta", { ...id, text: "补齐前缀" });
  emit("preview.delta", { ...id, text: "后续" });
  emit("output_final", { ...id, text: "补齐前缀后续" });
  expect(store.getState().runtime.sessions.child.committed.output).toBe("补齐前缀后续");
  expect(store.getState().runtime.sessions.child.activePreview).toBeNull();
  close();
  store.dispatch(helpermeApi.util.resetApiState());
});
