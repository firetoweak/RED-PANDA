import {
  authorizationRequiredEventSchema,
  autoAuthorizeChangedEventSchema,
  connectedEventSchema,
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
import { redpandaApi } from "../api/redpandaApi";
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

  source.addEventListener("connected", (event) => {
    const payload = connectedEventSchema.parse(JSON.parse(event.data));
    dispatch(connected(payload.connection_id));
    dispatch(redpandaApi.util.invalidateTags(["Conversation", "Agents", "Groups", "Inbox", "AutoAuthorize"]));
  });
  source.addEventListener("auto_authorize_changed", (event) => {
    autoAuthorizeChangedEventSchema.parse(JSON.parse(event.data));
    dispatch(redpandaApi.util.invalidateTags(["AutoAuthorize", "Conversation", "Inbox", "Agents"]));
  });
  source.addEventListener("session_activity", (event) => {
    const payload = sessionActivityEventSchema.parse(JSON.parse(event.data));
    dispatch(
      sessionActivity({
        sessionId: payload.session_id,
        activity: payload.activity,
      }),
    );
    dispatch(
      redpandaApi.util.invalidateTags([
        { type: "Conversation", id: payload.session_id },
        "Agents",
        "Inbox",
      ]),
    );
  });
  source.addEventListener("schedule_changed", (event) => {
    const payload = scheduleChangedEventSchema.parse(JSON.parse(event.data));
    dispatch(
      redpandaApi.util.invalidateTags([
        { type: "Conversation", id: payload.session_id },
        { type: "Routines", id: payload.session_id },
        "Inbox",
      ]),
    );
  });
  source.addEventListener("group_changed", (event) => {
    groupChangedEventSchema.parse(JSON.parse(event.data));
    dispatch(redpandaApi.util.invalidateTags(["Groups"]));
  });
  source.addEventListener("session_failed", (event) => {
    const payload = sessionFailedEventSchema.parse(JSON.parse(event.data));
    dispatch(
      sessionFailed({
        sessionId: payload.session_id,
        message: payload.message,
      }),
    );
    dispatch(redpandaApi.util.invalidateTags(["Agents", "Inbox"]));
  });
  source.addEventListener("context_usage", (event) => {
    const payload = contextUsageEventSchema.parse(JSON.parse(event.data));
    dispatch(
      redpandaApi.util.invalidateTags([
        { type: "Conversation", id: payload.session_id },
      ]),
    );
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
    dispatch(
      redpandaApi.util.invalidateTags([
        { type: "Conversation", id: payload.session_id },
      ]),
    );
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
    dispatch(
      redpandaApi.util.invalidateTags([
        { type: "Conversation", id: payload.session_id },
      ]),
    );
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
    dispatch(
      redpandaApi.util.invalidateTags([
        { type: "Conversation", id: payload.session_id },
        "Inbox",
      ]),
    );
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
    dispatch(
      redpandaApi.util.invalidateTags([
        { type: "Conversation", id: payload.session_id },
      ]),
    );
  });
  source.addEventListener("authorization_required", (event) => {
    const payload = authorizationRequiredEventSchema.parse(JSON.parse(event.data));
    dispatch(authorizationRequired({ sessionId: payload.session_id }));
    dispatch(
      redpandaApi.util.invalidateTags([
        { type: "Conversation", id: payload.session_id },
        "Inbox",
      ]),
    );
  });
  source.addEventListener("error", () => {
    preview.flushNow();
    thinking.flushNow();
    dispatch(disconnected());
  });

  return () => {
    preview.flushNow();
    thinking.flushNow();
    source.close();
    dispatch(disconnected());
  };
}
