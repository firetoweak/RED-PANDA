import { createSlice, type PayloadAction } from "@reduxjs/toolkit";

export type SessionActivity = "running" | "idle";

export type ActivePreview = {
  outputId: string;
  text: string;
};

export type LiveTool = {
  commandId: string;
  name: string;
  status: "running";
};

export type ToolProgressStatus = "running" | "settled";

export type SessionRuntime = {
  activity: SessionActivity | null;
  lastError: string | null;
  activePreview: ActivePreview | null;
  activeThinking: ActivePreview | null;
  unread: number;
  committed: Record<string, string>;
  committedThinking: Record<string, string>;
  tools: Record<string, LiveTool>;
  controlNotice: string | null;
  conversationStatus: {
    compactCount: number;
    compactPhase: "running" | "ready" | "failed" | null;
  } | null;
};

export type RuntimeState = {
  connectionId: string | null;
  viewingSessionId: string | null;
  ownerSessionId: string | null;
  sessions: Record<string, SessionRuntime>;
};

const initialState: RuntimeState = {
  connectionId: null,
  viewingSessionId: null,
  ownerSessionId: null,
  sessions: {},
};

function runtimeOf(state: RuntimeState, sessionId: string): SessionRuntime {
  const current = state.sessions[sessionId];
  if (current !== undefined) {
    return current;
  }
  const created: SessionRuntime = {
    activity: null,
    lastError: null,
    activePreview: null,
    activeThinking: null,
    unread: 0,
    committed: {},
    committedThinking: {},
    tools: {},
    controlNotice: null,
    conversationStatus: null,
  };
  state.sessions[sessionId] = created;
  return created;
}

const runtimeSlice = createSlice({
  name: "runtime",
  initialState,
  reducers: {
    connected(state, action: PayloadAction<string>) {
      state.connectionId = action.payload;
    },
    disconnected(state) {
      state.connectionId = null;
      state.ownerSessionId = null;
      for (const session of Object.values(state.sessions)) {
        session.activity = null;
        session.activePreview = null;
        session.activeThinking = null;
        session.committedThinking = {};
        session.tools = {};
      }
    },
    viewing(state, action: PayloadAction<string | null>) {
      state.viewingSessionId = action.payload;
      if (action.payload !== null) {
        runtimeOf(state, action.payload).unread = 0;
      }
    },
    bindOwner(state, action: PayloadAction<string>) {
      state.ownerSessionId = action.payload;
    },
    clearLiveOutput(state, action: PayloadAction<string>) {
      const session = runtimeOf(state, action.payload);
      session.lastError = null;
      session.activePreview = null;
      session.activeThinking = null;
      session.committed = {};
      session.committedThinking = {};
      session.tools = {};
      session.controlNotice = null;
      session.conversationStatus = null;
    },
    controlNotice(
      state,
      action: PayloadAction<{ sessionId: string; message: string | null }>,
    ) {
      runtimeOf(state, action.payload.sessionId).controlNotice =
        action.payload.message;
    },
    sessionActivity(
      state,
      action: PayloadAction<{ sessionId: string; activity: SessionActivity }>,
    ) {
      const session = runtimeOf(state, action.payload.sessionId);
      session.activity = action.payload.activity;
      if (action.payload.activity === "running") {
        session.lastError = null;
      } else {
        session.tools = {};
      }
    },
    sessionFailed(
      state,
      action: PayloadAction<{ sessionId: string; message: string }>,
    ) {
      const session = runtimeOf(state, action.payload.sessionId);
      session.lastError = action.payload.message;
      session.activity = "idle";
      session.tools = {};
    },
    previewStarted(
      state,
      action: PayloadAction<{ sessionId: string; outputId: string }>,
    ) {
      const session = runtimeOf(state, action.payload.sessionId);
      session.activePreview = { outputId: action.payload.outputId, text: "" };
    },
    previewDelta(
      state,
      action: PayloadAction<{ sessionId: string; outputId: string; text: string }>,
    ) {
      const session = runtimeOf(state, action.payload.sessionId);
      if (
        session.activePreview === null ||
        session.activePreview.outputId !== action.payload.outputId
      ) {
        return;
      }
      session.activePreview.text += action.payload.text;
    },
    previewAborted(
      state,
      action: PayloadAction<{ sessionId: string; outputId: string }>,
    ) {
      const session = runtimeOf(state, action.payload.sessionId);
      if (session.activePreview?.outputId === action.payload.outputId) {
        session.activePreview = null;
      }
    },
    thinkingStarted(
      state,
      action: PayloadAction<{ sessionId: string; outputId: string }>,
    ) {
      const session = runtimeOf(state, action.payload.sessionId);
      session.activeThinking = { outputId: action.payload.outputId, text: "" };
    },
    thinkingDelta(
      state,
      action: PayloadAction<{ sessionId: string; outputId: string; text: string }>,
    ) {
      const session = runtimeOf(state, action.payload.sessionId);
      if (
        session.activeThinking === null ||
        session.activeThinking.outputId !== action.payload.outputId
      ) {
        return;
      }
      session.activeThinking.text += action.payload.text;
    },
    thinkingClosed(
      state,
      action: PayloadAction<{ sessionId: string; outputId: string }>,
    ) {
      const session = runtimeOf(state, action.payload.sessionId);
      if (session.activeThinking?.outputId !== action.payload.outputId) {
        return;
      }
      if (session.activeThinking.text.trim() !== "") {
        session.committedThinking[action.payload.outputId] =
          session.activeThinking.text;
      }
      session.activeThinking = null;
    },
    outputFinal(
      state,
      action: PayloadAction<{ sessionId: string; outputId: string; text: string }>,
    ) {
      const session = runtimeOf(state, action.payload.sessionId);
      session.committed[action.payload.outputId] = action.payload.text;
      if (session.activePreview?.outputId === action.payload.outputId) {
        session.activePreview = null;
      }
      if (state.viewingSessionId !== action.payload.sessionId) {
        session.unread += 1;
      }
    },
    toolProgress(
      state,
      action: PayloadAction<{
        sessionId: string;
        commandId: string;
        name: string;
        status: ToolProgressStatus;
      }>,
    ) {
      const session = runtimeOf(state, action.payload.sessionId);
      if (action.payload.status === "settled") {
        delete session.tools[action.payload.commandId];
        return;
      }
      session.tools[action.payload.commandId] = {
        commandId: action.payload.commandId,
        name: action.payload.name,
        status: "running",
      };
    },
    authorizationRequired(state, action: PayloadAction<{ sessionId: string }>) {
      const { sessionId } = action.payload;
      if (state.viewingSessionId !== sessionId) {
        runtimeOf(state, sessionId).unread += 1;
      }
    },
    conversationStatus(
      state,
      action: PayloadAction<{
        sessionId: string;
        compactCount: number;
        compactPhase: "running" | "ready" | "failed" | null;
      }>,
    ) {
      runtimeOf(state, action.payload.sessionId).conversationStatus = {
        compactCount: action.payload.compactCount,
        compactPhase: action.payload.compactPhase,
      };
    },
  },
});

export const {
  connected,
  disconnected,
  viewing,
  bindOwner,
  clearLiveOutput,
  controlNotice,
  sessionActivity,
  sessionFailed,
  previewStarted,
  previewDelta,
  previewAborted,
  thinkingStarted,
  thinkingDelta,
  thinkingClosed,
  outputFinal,
  toolProgress,
  authorizationRequired,
  conversationStatus,
} = runtimeSlice.actions;
export default runtimeSlice.reducer;
