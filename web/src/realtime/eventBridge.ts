import {
  authorizationRequiredEventSchema,
  autoAuthorizeChangedEventSchema,
  avatarsChangedEventSchema,
  connectedEventSchema,
  externalSessionsChangedEventSchema,
  contextUsageEventSchema,
  conversationStatusEventSchema,
  outputFinalEventSchema,
  previewAbortedEventSchema,
  previewDeltaEventSchema,
  previewStartedEventSchema,
  groupChangedEventSchema,
  scheduleChangedEventSchema,
  sessionActivityEventSchema,
  sessionFailedEventSchema,
  thinkingDeltaEventSchema,
  thinkingFinishedEventSchema,
  thinkingStartedEventSchema,
  toolProgressEventSchema,
} from "../api/contracts";
import { kondenApi } from "../api/kondenApi";
import type { AppDispatch } from "../app/store";
import {
  authorizationRequired,
  connected,
  conversationStatus,
  disconnected,
  outputFinal,
  previewAborted,
  previewDelta,
  previewStarted,
  sessionActivity,
  sessionFailed,
  thinkingClosed,
  thinkingDelta,
  thinkingStarted,
  toolProgress,
} from "./runtimeSlice";
import { createTextDeltaBuffer } from "./textDeltaBuffer";

export function openEventBridge(dispatch: AppDispatch): () => void {
  const source = new EventSource("/api/events");
  const preview = createTextDeltaBuffer((delta) => {
    dispatch(previewDelta(delta));
  });
  const thinking = createTextDeltaBuffer((delta) => {
    dispatch(thinkingDelta(delta));
  });
  type Tags = Parameters<typeof kondenApi.util.invalidateTags>[0];
  let pendingTags: Tags = [];
  let refreshTimer: ReturnType<typeof setTimeout> | undefined;

  function flushRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = undefined;
    const tags = pendingTags;
    pendingTags = [];
    if (tags.length > 0) dispatch(kondenApi.util.invalidateTags(tags));
  }

  function refresh(tags: Tags) {
    pendingTags = [...pendingTags, ...tags];
    refreshTimer ??= setTimeout(flushRefresh, 50);
  }

  source.addEventListener("connected", (event) => {
    const payload = connectedEventSchema.parse(JSON.parse(event.data));
    dispatch(connected(payload.connection_id));
    refresh(["Conversation", "Agents", "Groups", "Inbox", "AutoAuthorize", "Avatars", "ExternalSessions", "ExternalConnections"]);
    flushRefresh();
  });
  source.addEventListener("external_sessions_changed", (event) => {
    externalSessionsChangedEventSchema.parse(JSON.parse(event.data));
    refresh(["ExternalSessions", "Conversation"]);
  });
  source.addEventListener("avatars_changed", (event) => {
    avatarsChangedEventSchema.parse(JSON.parse(event.data));
    refresh(["Avatars"]);
  });
  source.addEventListener("auto_authorize_changed", (event) => {
    autoAuthorizeChangedEventSchema.parse(JSON.parse(event.data));
    refresh(["AutoAuthorize", "Conversation", "Inbox", "Agents"]);
  });
  source.addEventListener("session_activity", (event) => {
    const payload = sessionActivityEventSchema.parse(JSON.parse(event.data));
    dispatch(
      sessionActivity({
        sessionId: payload.session_id,
        activity: payload.activity,
      }),
    );
    refresh([
      { type: "Conversation", id: payload.session_id },
      "Agents",
      "Inbox",
    ]);
  });
  source.addEventListener("schedule_changed", (event) => {
    const payload = scheduleChangedEventSchema.parse(JSON.parse(event.data));
    refresh([
      { type: "Conversation", id: payload.session_id },
      { type: "Routines", id: payload.session_id },
      "Inbox",
    ]);
  });
  source.addEventListener("group_changed", (event) => {
    groupChangedEventSchema.parse(JSON.parse(event.data));
    refresh(["Groups"]);
  });
  source.addEventListener("session_failed", (event) => {
    const payload = sessionFailedEventSchema.parse(JSON.parse(event.data));
    dispatch(
      sessionFailed({
        sessionId: payload.session_id,
        message: payload.message,
      }),
    );
    refresh(["Agents", "Inbox"]);
  });
  source.addEventListener("context_usage", (event) => {
    const payload = contextUsageEventSchema.parse(JSON.parse(event.data));
    refresh([{ type: "Conversation", id: payload.session_id }]);
  });
  source.addEventListener("conversation_status", (event) => {
    const payload = conversationStatusEventSchema.parse(JSON.parse(event.data));
    dispatch(
      conversationStatus({
        sessionId: payload.session_id,
        compactCount: payload.compact_count,
        compactPhase: payload.compact_phase,
      }),
    );
    refresh([{ type: "Conversation", id: payload.session_id }]);
  });
  source.addEventListener("preview.started", (event) => {
    const payload = previewStartedEventSchema.parse(JSON.parse(event.data));
    preview.flushNow(payload.session_id);
    dispatch(
      previewStarted({
        sessionId: payload.session_id,
        outputId: payload.output_id,
      }),
    );
    // Tool finish can precede Outcome commit. The next decision observes committed
    // facts, so refresh the plan and timeline before waiting for model output.
    refresh([{ type: "Conversation", id: payload.session_id }]);
    flushRefresh();
  });
  source.addEventListener("preview.delta", (event) => {
    const payload = previewDeltaEventSchema.parse(JSON.parse(event.data));
    preview.enqueue({
      sessionId: payload.session_id,
      outputId: payload.output_id,
      text: payload.text,
    });
  });
  source.addEventListener("preview.aborted", (event) => {
    const payload = previewAbortedEventSchema.parse(JSON.parse(event.data));
    preview.flushNow(payload.session_id);
    dispatch(
      previewAborted({
        sessionId: payload.session_id,
        outputId: payload.output_id,
      }),
    );
  });
  source.addEventListener("thinking.started", (event) => {
    const payload = thinkingStartedEventSchema.parse(JSON.parse(event.data));
    thinking.flushNow(payload.session_id);
    dispatch(
      thinkingStarted({
        sessionId: payload.session_id,
        outputId: payload.output_id,
      }),
    );
  });
  source.addEventListener("thinking.delta", (event) => {
    const payload = thinkingDeltaEventSchema.parse(JSON.parse(event.data));
    thinking.enqueue({
      sessionId: payload.session_id,
      outputId: payload.output_id,
      text: payload.text,
    });
  });
  source.addEventListener("thinking.finished", (event) => {
    const payload = thinkingFinishedEventSchema.parse(JSON.parse(event.data));
    thinking.flushNow(payload.session_id);
    dispatch(
      thinkingClosed({
        sessionId: payload.session_id,
        outputId: payload.output_id,
      }),
    );
  });
  source.addEventListener("thinking.aborted", (event) => {
    const payload = thinkingFinishedEventSchema.parse(JSON.parse(event.data));
    thinking.flushNow(payload.session_id);
    dispatch(
      thinkingClosed({
        sessionId: payload.session_id,
        outputId: payload.output_id,
      }),
    );
  });
  source.addEventListener("output_final", (event) => {
    const payload = outputFinalEventSchema.parse(JSON.parse(event.data));
    preview.flushNow(payload.session_id);
    dispatch(
      outputFinal({
        sessionId: payload.session_id,
        outputId: payload.output_id,
        text: payload.text,
      }),
    );
    refresh([
      { type: "Conversation", id: payload.session_id },
      "Inbox",
    ]);
  });
  source.addEventListener("tool_progress", (event) => {
    const payload = toolProgressEventSchema.parse(JSON.parse(event.data));
    dispatch(
      toolProgress({
        sessionId: payload.session_id,
        commandId: payload.command_id,
        name: payload.name,
        status: payload.status,
      }),
    );
    refresh([{ type: "Conversation", id: payload.session_id }]);
  });
  source.addEventListener("authorization_required", (event) => {
    const payload = authorizationRequiredEventSchema.parse(JSON.parse(event.data));
    dispatch(authorizationRequired({ sessionId: payload.session_id }));
    refresh([
      { type: "Conversation", id: payload.session_id },
      "Inbox",
    ]);
  });
  source.addEventListener("error", () => {
    preview.flushNow();
    thinking.flushNow();
    dispatch(disconnected());
  });

  return () => {
    clearTimeout(refreshTimer);
    preview.flushNow();
    thinking.flushNow();
    source.close();
    dispatch(disconnected());
  };
}
