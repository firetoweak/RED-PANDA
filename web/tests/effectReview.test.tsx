import { MantineProvider } from "@mantine/core";
import { configureStore } from "@reduxjs/toolkit";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Provider } from "react-redux";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ConversationView, EffectReview } from "../src/api/contracts";
import { redpandaApi } from "../src/api/redpandaApi";
import { Conversation } from "../src/features/conversation/Conversation";
import { EffectReviewCanvas } from "../src/features/conversation/EffectReviewCanvas";
import {
  EFFECT_REVIEW_SAMPLE_STORAGE_KEY,
  resolveEffectReview,
  sampleEffectReview,
} from "../src/features/conversation/effectReview";
import { bindOwner, connected, sessionActivity } from "../src/realtime/runtimeSlice";
import runtimeReducer from "../src/realtime/runtimeSlice";

vi.mock("../src/features/conversation/MarkdownMessage", () => ({
  MarkdownMessage: ({ content }: { content: string }) => <span>{content}</span>,
}));
vi.mock("../src/features/conversation/Composer", () => ({ Composer: () => null }));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.removeItem(EFFECT_REVIEW_SAMPLE_STORAGE_KEY);
});

const review: EffectReview = {
  conclusion: "启动耗时从 1.8s 降到 0.4s。",
  metrics: [
    { label: "启动耗时", before: "1.8", after: "0.4", unit: "s" },
    { label: "冷启动请求", before: "3", after: "1", unit: null },
  ],
  changes: [
    { path: "web/src/main.tsx", reason: "去掉重复的主题初始化" },
    { path: "redpanda/channels/web/app.py", reason: "静态资源只挂一次" },
  ],
  actions: [
    { kind: "authorize", label: "允许执行检查", command_id: "cmd-check", approved: true },
    { kind: "restore", label: "从这一步之后重开", step_id: "reply-step" },
    { kind: "todo", label: "接受并发布", note: "接受并发布还没有单独的操作入口。" },
  ],
};

describe("resolveEffectReview", () => {
  const step = {
    pending: false,
    stepId: "reply-step",
    rewindable: true,
    effectReview: null,
  };

  it("uses a committed review and ignores preview text", () => {
    expect(resolveEffectReview({ ...step, effectReview: review }, { sample: false })?.source)
      .toBe("projection");
    expect(resolveEffectReview({ ...step, pending: true, effectReview: review }, { sample: true }))
      .toBeNull();
  });

  it("overlays the sample only on a settled committed step that has no review", () => {
    expect(resolveEffectReview(step, { sample: false })).toBeNull();
    expect(resolveEffectReview(step, { sample: true })).toEqual({
      review: sampleEffectReview(step),
      source: "sample",
    });
    expect(resolveEffectReview({ ...step, rewindable: false }, { sample: true })?.review.actions
      .map((action) => action.kind)).toEqual(["todo"]);
  });
});

it("renders metric cards, a collapsed change list, a host action, and a todo that does not call out", () => {
  vi.stubGlobal("matchMedia", () => ({
    matches: false, addEventListener() {}, removeEventListener() {},
  }));
  const onAuthorize = vi.fn();
  const onRestore = vi.fn();
  render(
    <MantineProvider>
      <EffectReviewCanvas
        disabled={false}
        onAuthorize={onAuthorize}
        onRestore={onRestore}
        prose={"函数 `boot()` 的表格很长。"}
        review={review}
        source="projection"
      />
    </MantineProvider>,
  );
  expect(screen.getByText("启动耗时从 1.8s 降到 0.4s。")).toBeInTheDocument();
  expect(screen.getByText("1.8 s")).toBeInTheDocument();
  expect(screen.getByText("0.4 s")).toBeInTheDocument();
  expect(screen.queryByText("web/src/main.tsx")).not.toBeInTheDocument();
  expect(screen.queryByText("函数 `boot()` 的表格很长。")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "改动 2 处" }));
  expect(screen.getByText("web/src/main.tsx")).toBeInTheDocument();
  expect(screen.getByText("静态资源只挂一次")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "模型原文" }));
  expect(screen.getByText("函数 `boot()` 的表格很长。")).toBeInTheDocument();

  const todo = screen.getByRole("button", { name: "接受并发布" });
  expect(todo).toBeDisabled();
  fireEvent.click(todo);
  expect(onAuthorize).not.toHaveBeenCalled();
  expect(onRestore).not.toHaveBeenCalled();
  expect(screen.getByText("接受并发布还没有单独的操作入口。")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "允许执行检查" }));
  expect(onAuthorize).toHaveBeenCalledWith("cmd-check", true);
  fireEvent.click(screen.getByRole("button", { name: "从这一步之后重开" }));
  expect(onRestore).toHaveBeenCalledWith("reply-step");
});

describe("conversation effect review", () => {
  const session = {
    status: "waiting",
    waiting_for: ["external_fact"],
    pending_authorization_ids: [],
    pending_authorization_commands: [],
    should_wake: false,
    has_active_subagents: false,
    control_approval: null,
    control_message: null,
    auto_authorize: false,
    paused: false,
  };

  function view(effectReview: EffectReview | null): ConversationView {
    return {
      session_id: "session",
      workspace_id: "workspace",
      revision: 2,
      compact_count: 0,
      compact_phase: null,
      waiting_until: null,
      workspace_version: null,
      context_input_tokens: null,
      work_plan: null,
      work_plan_updates: [],
      session,
      items: [
        {
          kind: "user",
          message_id: "user-1",
          text: "看一下启动",
          occurred_at: "2026-10-03T00:00:00Z",
          images: [],
          files: [],
        },
        {
          kind: "step",
          step_id: "reply-step",
          output_id: "reply-output",
          text: "函数 boot() 从 1.8s 降到 0.4s，改了 main.tsx。",
          thinking: null,
          occurred_at: "2026-10-03T00:00:02Z",
          rewindable: true,
          tools: [],
          ...(effectReview === null ? {} : { effect_review: effectReview }),
        },
      ],
    };
  }

  function renderConversation(
    data: ConversationView,
    prepare?: (store: ReturnType<typeof configureStore>) => void,
  ) {
    vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
    vi.stubGlobal("matchMedia", () => ({
      matches: false, addEventListener() {}, removeEventListener() {},
    }));
    const NativeRequest = Request;
    vi.stubGlobal("Request", class extends NativeRequest {
      constructor(input: RequestInfo | URL, init?: RequestInit) {
        super(typeof input === "string" ? new URL(input, "http://localhost") : input, init);
      }
    });
    const calls: { url: string; body: string | null }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      const body = typeof init?.body === "string"
        ? init.body
        : input instanceof Request ? await input.clone().text() : null;
      calls.push({ url, body });
      return Response.json(data);
    }));
    const store = configureStore({
      reducer: { runtime: runtimeReducer, [redpandaApi.reducerPath]: redpandaApi.reducer },
      middleware: (getDefaultMiddleware) => getDefaultMiddleware().concat(redpandaApi.middleware),
    });
    store.dispatch(connected("connection"));
    store.dispatch(bindOwner("session"));
    prepare?.(store);
    render(
      <Provider store={store}>
        <MantineProvider>
          <MemoryRouter initialEntries={["/sessions/session"]}>
            <Routes>
              <Route path="/sessions/:sessionId" element={<Conversation />} />
            </Routes>
          </MemoryRouter>
        </MantineProvider>
      </Provider>,
    );
    return { calls, store };
  }

  it("renders the canvas from a structured step and keeps markdown for a plain reply", async () => {
    const structured = renderConversation(view(review));
    expect(await screen.findByText("启动耗时从 1.8s 降到 0.4s。")).toBeInTheDocument();
    expect(screen.queryByText("函数 boot() 从 1.8s 降到 0.4s，改了 main.tsx。")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "允许执行检查" }));
    await waitFor(() => {
      expect(structured.calls.some((call) => call.url.includes(
        "/sessions/session/commands/cmd-check/authorize",
      ))).toBe(true);
    });
    const authorize = structured.calls.find((call) => call.url.includes("/authorize"));
    expect(JSON.parse(authorize?.body ?? "{}")).toMatchObject({
      connection_id: "connection",
      approved: true,
    });

    fireEvent.click(screen.getByRole("button", { name: "从这一步之后重开" }));
    expect(await screen.findByText(/工作区文件可以一起退回/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "会话和文件一起回滚" }));
    await waitFor(() => {
      expect(structured.calls.some((call) => call.url.includes("/sessions/session/restarts"))).toBe(true);
    });
    const restart = structured.calls.find((call) => call.url.includes("/restarts"));
    expect(JSON.parse(restart?.body ?? "{}")).toMatchObject({
      step_id: "reply-step",
      restore_files: true,
    });
    expect(structured.calls.some((call) => call.url.includes("tool"))).toBe(false);
    cleanup();
    structured.store.dispatch(redpandaApi.util.resetApiState());

    renderConversation(view(null));
    expect(await screen.findByText("函数 boot() 从 1.8s 降到 0.4s，改了 main.tsx。")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "效果核对" })).not.toBeInTheDocument();
  });

  it("shows the sample on a settled reply and keeps markdown while the session is still running", async () => {
    window.localStorage.setItem(EFFECT_REVIEW_SAMPLE_STORAGE_KEY, "sample");
    const running = renderConversation(view(null), (store) => {
      store.dispatch(sessionActivity({ sessionId: "session", activity: "running" }));
    });
    expect(await screen.findByText("函数 boot() 从 1.8s 降到 0.4s，改了 main.tsx。")).toBeInTheDocument();
    expect(screen.queryByText("样例：启动耗时从 1.8s 降到 0.4s，改动集中在启动路径。")).not.toBeInTheDocument();
    cleanup();
    running.store.dispatch(redpandaApi.util.resetApiState());

    renderConversation(view(null));
    expect(await screen.findByText("样例：启动耗时从 1.8s 降到 0.4s，改动集中在启动路径。")).toBeInTheDocument();
    expect(screen.getByText("实验样例")).toBeInTheDocument();
    expect(screen.queryByText("函数 boot() 从 1.8s 降到 0.4s，改了 main.tsx。")).not.toBeInTheDocument();
  });
});
