import { configureStore } from "@reduxjs/toolkit";
import { afterEach, expect, it, vi } from "vitest";
import { kondenApi } from "../src/api/kondenApi";
import { openEventBridge } from "../src/realtime/eventBridge";
import type { ConversationView } from "../src/api/contracts";
import { visibleTimeline } from "../src/features/conversation/visibleTimeline";
import runtimeReducer from "../src/realtime/runtimeSlice";

afterEach(() => vi.unstubAllGlobals());

// HTTP may overtake SSE, but the events of one SSE connection remain ordered.
it.each([0, 1, 2, 3, 4])("重连后最终快照插在第 %i 个流事件前，回复都只显示一次", (snapshotAt) => {
  const listeners = new Map<string, (event: MessageEvent) => void>();
  vi.stubGlobal("EventSource", class {
    addEventListener(name: string, listener: (event: MessageEvent) => void) { listeners.set(name, listener); }
    close() {}
  });
  const frames = new Map<number, FrameRequestCallback>();
  let nextFrame = 0;
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    frames.set(++nextFrame, callback);
    return nextFrame;
  });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => frames.delete(id));
  const store = configureStore({
    reducer: { runtime: runtimeReducer, [kondenApi.reducerPath]: kondenApi.reducer },
    middleware: (defaults) => defaults().concat(kondenApi.middleware),
  });
  const close = openEventBridge(store.dispatch);
  const emit = (name: string, payload: object = {}) => listeners.get(name)!(new MessageEvent(name, { data: JSON.stringify(payload) }));
  const id = { session_id: "Ada", output_id: "output" };
  const base: ConversationView = {
    session_id: "Ada", workspace_id: null, revision: 1,
    compact_count: 0, compact_phase: null, waiting_until: null, work_plan: null,
    work_plan_updates: [], context_input_tokens: null,
    deleted: false, agent_name: null,
    session: {
      status: "runnable", waiting_for: [], pending_authorization_ids: [], pending_authorization_commands: [],
      should_wake: true, has_open_subagents: false, control_approval: null, control_message: null,
      auto_authorize: false, last_error: null,
    },
    items: [{ kind: "user", message_id: "user", text: "你好", images: [], files: [], occurred_at: "2026-10-23T00:00:00Z" }],
  };
  const final: ConversationView = {
    ...base, revision: 3,
    items: [...base.items, { kind: "step", step_id: "step", output_id: "output", text: "完整回复",
      thinking: null, tools: [], occurred_at: "2026-10-23T00:00:01Z" }],
  };
  let conversation = base;
  const visibleReplies = () => {
    const runtime = store.getState().runtime.sessions.Ada;
    return visibleTimeline(conversation, runtime.committed, runtime.activePreview, runtime.tools,
      runtime.committedThinking, runtime.activeThinking, runtime.activity).filter((item) => item.kind === "step");
  };
  emit("connected", { connection_id: "old" });
  emit("preview.started", id);
  emit("preview.delta", { ...id, text: "旧连接残片" });
  emit("error");
  expect(frames.size).toBe(0);
  expect(visibleReplies()).toEqual([]);
  emit("connected", { connection_id: "new" });
  emit("session_activity", { session_id: "Ada", activity: "running" });
  const events = [
    () => emit("preview.started", id),
    () => emit("preview.delta", { ...id, text: "完整" }),
    () => emit("preview.delta", { ...id, text: "回复" }),
    () => emit("output_final", { ...id, text: "完整回复" }),
  ];
  for (let position = 0; position <= events.length; position += 1) {
    if (position === snapshotAt) conversation = final;
    if (position < events.length) events[position]();
    if (position >= snapshotAt) expect(visibleReplies()).toMatchObject([{ outputId: "output", text: "完整回复" }]);
  }
  for (const callback of frames.values()) callback(0);
  expect(frames.size).toBe(0);
  expect(store.getState().runtime.sessions.Ada.activePreview).toBeNull();
  expect(store.getState().runtime.sessions.Ada.committed).toEqual({ output: "完整回复" });
  expect(visibleReplies()).toMatchObject([{ outputId: "output", text: "完整回复" }]);
  close();
  store.dispatch(kondenApi.util.resetApiState());
});

it("断线清掉旧流，重连快照与后续增量在相同输出身份下衔接", () => {
  const listeners = new Map<string, (event: MessageEvent) => void>();
  vi.stubGlobal("EventSource", class {
    addEventListener(name: string, listener: (event: MessageEvent) => void) { listeners.set(name, listener); }
    close() {}
  });
  const store = configureStore({
    reducer: { runtime: runtimeReducer, [kondenApi.reducerPath]: kondenApi.reducer },
    middleware: (getDefaultMiddleware) => getDefaultMiddleware().concat(kondenApi.middleware),
  });
  const close = openEventBridge(store.dispatch);
  const emit = (name: string, payload: object = {}) => listeners.get(name)!(new MessageEvent(name, { data: JSON.stringify(payload) }));
  const id = { session_id: "child", output_id: "output" };
  emit("connected", { connection_id: "first" });
  emit("session_activity", { session_id: "child", activity: "running" });
  emit("preview.started", { ...id });
  emit("preview.delta", { ...id, text: "旧片段" });
  emit("thinking.started", { ...id });
  emit("thinking.delta", { ...id, text: "旧思考" });
  emit("error");
  expect(store.getState().runtime.sessions.child.activePreview).toBeNull();
  expect(store.getState().runtime.sessions.child.activeThinking).toBeNull();
  expect(store.getState().runtime.sessions.child.activity).toBeNull();
  emit("connected", { connection_id: "second" });
  emit("session_activity", { session_id: "child", activity: "running" });
  emit("preview.started", { ...id });
  emit("preview.delta", { ...id, text: "补齐前缀" });
  emit("preview.delta", { ...id, text: "后续" });
  emit("output_final", { ...id, text: "补齐前缀后续" });
  expect(store.getState().runtime.sessions.child.committed.output).toBe("补齐前缀后续");
  expect(store.getState().runtime.sessions.child.activePreview).toBeNull();
  close();
  store.dispatch(kondenApi.util.resetApiState());
});

it("群变化事件让群数据重新读取，调度结束后页面不会停在旧的等待状态", () => {
  const listeners = new Map<string, (event: MessageEvent) => void>();
  vi.stubGlobal("EventSource", class {
    addEventListener(name: string, listener: (event: MessageEvent) => void) { listeners.set(name, listener); }
    close() {}
  });
  const dispatch = vi.fn();
  const close = openEventBridge(dispatch);
  listeners.get("group_changed")!(new MessageEvent("group_changed", { data: JSON.stringify({ group_id: "group-1" }) }));
  expect(dispatch).toHaveBeenCalledWith(kondenApi.util.invalidateTags(["Groups"]));
  listeners.get("auto_authorize_changed")!(new MessageEvent("auto_authorize_changed", { data: "{}" }));
  expect(dispatch).toHaveBeenCalledWith(kondenApi.util.invalidateTags(["AutoAuthorize", "Conversation", "Inbox", "Agents"]));
  listeners.get("avatars_changed")!(new MessageEvent("avatars_changed", { data: "{}" }));
  expect(dispatch).toHaveBeenCalledWith(kondenApi.util.invalidateTags(["Avatars"]));
  listeners.get("connected")!(new MessageEvent("connected", { data: '{"connection_id":"again"}' }));
  expect(dispatch).toHaveBeenCalledWith(kondenApi.util.invalidateTags(["Conversation", "Agents", "Groups", "Inbox", "AutoAuthorize", "Avatars"]));
  close();
});
