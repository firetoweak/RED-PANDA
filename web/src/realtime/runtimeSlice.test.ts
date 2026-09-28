import { describe, expect, it } from "vitest";

import reducer, {
  bindOwner,
  connected,
  contextUsage,
  controlNotice,
  conversationStatus,
  disconnected,
  lockDraft,
  outputFinal,
  previewDelta,
  previewStarted,
  sessionActivity,
  sessionFailed,
  thinkingClosed,
  thinkingDelta,
  thinkingStarted,
  setDraftSession,
  clearLiveOutput,
  toolProgress,
  viewing,
} from "./runtimeSlice";

describe("runtimeSlice", () => {
  it("appends preview deltas and commits final text on the same output identity", () => {
    let state = reducer(undefined, previewStarted({ sessionId: "s1", outputId: "out" }));
    state = reducer(
      state,
      previewDelta({ sessionId: "s1", outputId: "out", text: "你" }),
    );
    state = reducer(
      state,
      previewDelta({ sessionId: "s1", outputId: "out", text: "好" }),
    );
    expect(state.sessions.s1.activePreview).toEqual({ outputId: "out", text: "你好" });

    state = reducer(
      state,
      outputFinal({ sessionId: "s1", outputId: "out", text: "你好" }),
    );
    expect(state.sessions.s1.activePreview).toBeNull();
    expect(state.sessions.s1.committed.out).toBe("你好");
    expect(state.sessions.s1.unread).toBe(1);
  });

  it("streams thinking separately from assistant preview text", () => {
    let state = reducer(
      undefined,
      thinkingStarted({ sessionId: "s1", outputId: "out" }),
    );
    state = reducer(
      state,
      thinkingDelta({ sessionId: "s1", outputId: "out", text: "想" }),
    );
    expect(state.sessions.s1.activeThinking).toEqual({
      outputId: "out",
      text: "想",
    });
    expect(state.sessions.s1.activePreview).toBeNull();

    state = reducer(state, thinkingClosed({ sessionId: "s1", outputId: "out" }));
    expect(state.sessions.s1.activeThinking).toBeNull();
    expect(state.sessions.s1.committedThinking.out).toBe("想");
  });

  it("does not treat preview or thinking as an idle activity overlay", () => {
    let state = reducer(
      undefined,
      thinkingStarted({ sessionId: "s1", outputId: "out" }),
    );
    expect(state.sessions.s1.activity).toBeNull();

    state = reducer(state, previewStarted({ sessionId: "s1", outputId: "out" }));
    expect(state.sessions.s1.activity).toBeNull();

    state = reducer(
      state,
      sessionActivity({ sessionId: "s1", activity: "running" }),
    );
    expect(state.sessions.s1.activity).toBe("running");
  });

  it("marks unread only when the session is not being viewed", () => {
    let state = reducer(undefined, viewing("s1"));
    state = reducer(state, sessionActivity({ sessionId: "s1", activity: "running" }));
    state = reducer(
      state,
      outputFinal({ sessionId: "s1", outputId: "out", text: "在看" }),
    );
    expect(state.sessions.s1.unread).toBe(0);

    state = reducer(state, viewing("s2"));
    state = reducer(
      state,
      outputFinal({ sessionId: "s1", outputId: "out-2", text: "离开后" }),
    );
    expect(state.sessions.s1.unread).toBe(1);
    expect(state.sessions.s2.unread).toBe(0);
  });

  it("keeps only exact running activity and clears it when the handler settles", () => {
    let state = reducer(undefined, viewing("s2"));
    state = reducer(
      state,
      toolProgress({
        sessionId: "s1",
        commandId: "cmd-1",
        name: "read_file",
        status: "running",
      }),
    );
    state = reducer(
      state,
      toolProgress({
        sessionId: "s1",
        commandId: "cmd-1",
        name: "read_file",
        status: "settled",
      }),
    );

    expect(state.sessions.s1.tools).toEqual({});
    expect(state.sessions.s2.tools).toEqual({});
    expect(state.sessions.s1.unread).toBe(0);
  });

  it("drops exact tool activity when the session becomes idle", () => {
    let state = reducer(
      undefined,
      toolProgress({
        sessionId: "s1",
        commandId: "cmd-1",
        name: "read_file",
        status: "running",
      }),
    );
    state = reducer(state, sessionActivity({ sessionId: "s1", activity: "idle" }));
    expect(state.sessions.s1.tools).toEqual({});
  });

  it("keeps a run failure off the timeline and clears it when the session runs again", () => {
    let state = reducer(
      undefined,
      sessionFailed({
        sessionId: "s1",
        message: "运行失败：模型服务暂时不可用",
      }),
    );
    expect(state.sessions.s1.lastError).toBe("运行失败：模型服务暂时不可用");
    expect(state.sessions.s1.committed).toEqual({});

    state = reducer(
      state,
      sessionFailed({
        sessionId: "s1",
        message: "运行失败：模型请求失败",
      }),
    );
    expect(state.sessions.s1.lastError).toBe("运行失败：模型请求失败");

    state = reducer(state, sessionActivity({ sessionId: "s1", activity: "idle" }));
    expect(state.sessions.s1.lastError).toBe("运行失败：模型请求失败");

    state = reducer(state, sessionActivity({ sessionId: "s1", activity: "running" }));
    expect(state.sessions.s1.lastError).toBeNull();
  });

  it("stores context usage on the owning session", () => {
    let state = reducer(undefined, viewing("s2"));
    state = reducer(
      state,
      contextUsage({ sessionId: "s1", used: 1200, compact_threshold_tokens: 200000 }),
    );
    expect(state.sessions.s1.contextUsage).toEqual({ used: 1200, compact_threshold_tokens: 200000 });
    expect(state.sessions.s2.contextUsage).toBeNull();
  });

  it("stores compact status on the owning session", () => {
    let state = reducer(
      undefined,
      conversationStatus({
        sessionId: "s1",
        compactCount: 1,
        compactPhase: "failed",
      }),
    );
    expect(state.sessions.s1.conversationStatus).toEqual({
      compactCount: 1,
      compactPhase: "failed",
    });
  });

  it("keeps an unlocked draft per workspace until the first user message locks it", () => {
    let state = reducer(
      undefined,
      setDraftSession({ workspaceId: "w1", sessionId: "draft-1" }),
    );
    state = reducer(
      state,
      setDraftSession({ workspaceId: "w2", sessionId: "draft-2" }),
    );
    expect(state.draftSessions).toEqual({ w1: "draft-1", w2: "draft-2" });
    state = reducer(state, lockDraft("other"));
    expect(state.draftSessions).toEqual({ w1: "draft-1", w2: "draft-2" });
    state = reducer(state, lockDraft("draft-1"));
    expect(state.draftSessions).toEqual({ w2: "draft-2" });
  });

  it("clears the host owner on disconnect without dropping unlocked drafts", () => {
    let state = reducer(undefined, connected("conn-1"));
    state = reducer(
      state,
      setDraftSession({ workspaceId: "w1", sessionId: "draft-1" }),
    );
    state = reducer(state, bindOwner("draft-1"));
    state = reducer(
      state,
      toolProgress({
        sessionId: "draft-1",
        commandId: "cmd-1",
        name: "read_file",
        status: "running",
      }),
    );
    state = reducer(state, disconnected());
    expect(state.connectionId).toBeNull();
    expect(state.ownerSessionId).toBeNull();
    expect(state.draftSessions).toEqual({ w1: "draft-1" });
    expect(state.sessions["draft-1"].tools).toEqual({});
  });

  it("keeps a control notice after idle so install results survive refetch", () => {
    let state = reducer(
      undefined,
      controlNotice({
        sessionId: "s1",
        message: "MCP Server `demo` 安装、测试并启用成功。",
      }),
    );
    state = reducer(state, sessionActivity({ sessionId: "s1", activity: "idle" }));
    expect(state.sessions.s1.controlNotice).toBe(
      "MCP Server `demo` 安装、测试并启用成功。",
    );

    state = reducer(state, clearLiveOutput("s1"));
    expect(state.sessions.s1.controlNotice).toBeNull();
  });

  it("clears live output so a truncated edit does not keep later previews", () => {
    let state = reducer(undefined, previewStarted({ sessionId: "s1", outputId: "out" }));
    state = reducer(state, outputFinal({ sessionId: "s1", outputId: "out", text: "old" }));
    state = reducer(state, clearLiveOutput("s1"));
    expect(state.sessions.s1.activePreview).toBeNull();
    expect(state.sessions.s1.committed).toEqual({});
  });
});
