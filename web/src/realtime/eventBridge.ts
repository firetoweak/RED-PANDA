import {
  authorizationRequiredEventSchema,
  connectedEventSchema,
  contextUsageEventSchema,
  conversationStatusEventSchema,
  outputFinalEventSchema,
  previewAbortedEventSchema,
  previewDeltaEventSchema,
  previewStartedEventSchema,
  scheduleChangedEventSchema,
  sessionActivityEventSchema,
  sessionFailedEventSchema,
  thinkingDeltaEventSchema,
  thinkingFinishedEventSchema,
  thinkingStartedEventSchema,
  toolProgressEventSchema,
} from "../api/contracts";
import { helpermeApi } from "../api/helpermeApi";
import type { AppDispatch } from "../app/store";
import {
  authorizationRequired,
  connected,
  contextUsage,
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
      helpermeApi.util.invalidateTags([
        { type: "Conversation", id: payload.session_id },
        "Sessions",
      ]),
    );
  });
  source.addEventListener("schedule_changed", (event) => {
    const payload = scheduleChangedEventSchema.parse(JSON.parse(event.data));
    dispatch(
      helpermeApi.util.invalidateTags([
        { type: "Conversation", id: payload.session_id },
      ]),
    );
  });
  source.addEventListener("session_failed", (event) => {
    const payload = sessionFailedEventSchema.parse(JSON.parse(event.data));
    dispatch(
      sessionFailed({
        sessionId: payload.session_id,
        message: payload.message,
      }),
    );
  });
  source.addEventListener("context_usage", (event) => {
    const payload = contextUsageEventSchema.parse(JSON.parse(event.data));
    dispatch(
      contextUsage({
        sessionId: payload.session_id,
        used: payload.used,
        compact_threshold_tokens: payload.compact_threshold_tokens,
      }),
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
  });
  source.addEventListener("preview.started", (event) => {
    const payload = previewStartedEventSchema.parse(JSON.parse(event.data));
    preview.flushNow();
    dispatch(
      previewStarted({
        sessionId: payload.session_id,
        outputId: payload.output_id,
      }),
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
    preview.flushNow();
    dispatch(
      previewAborted({
        sessionId: payload.session_id,
        outputId: payload.output_id,
      }),
    );
  });
  source.addEventListener("thinking.started", (event) => {
    const payload = thinkingStartedEventSchema.parse(JSON.parse(event.data));
    thinking.flushNow();
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
    thinking.flushNow();
    dispatch(
      thinkingClosed({
        sessionId: payload.session_id,
        outputId: payload.output_id,
      }),
    );
  });
  source.addEventListener("thinking.aborted", (event) => {
    const payload = thinkingFinishedEventSchema.parse(JSON.parse(event.data));
    thinking.flushNow();
    dispatch(
      thinkingClosed({
        sessionId: payload.session_id,
        outputId: payload.output_id,
      }),
    );
  });
  source.addEventListener("output_final", (event) => {
    const payload = outputFinalEventSchema.parse(JSON.parse(event.data));
    preview.flushNow();
    dispatch(
      outputFinal({
        sessionId: payload.session_id,
        outputId: payload.output_id,
        text: payload.text,
      }),
    );
    dispatch(
      helpermeApi.util.invalidateTags([
        { type: "Conversation", id: payload.session_id },
        "Sessions",
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
      helpermeApi.util.invalidateTags([
        { type: "Conversation", id: payload.session_id },
      ]),
    );
  });
  source.addEventListener("authorization_required", (event) => {
    const payload = authorizationRequiredEventSchema.parse(JSON.parse(event.data));
    dispatch(
      authorizationRequired({
        sessionId: payload.session_id,
        commandId: payload.command_id,
        name: payload.name,
        arguments: payload.arguments,
      }),
    );
    dispatch(
      helpermeApi.util.invalidateTags([
        { type: "Conversation", id: payload.session_id },
      ]),
    );
  });
  source.addEventListener("error", () => {
    dispatch(disconnected());
  });

  return () => {
    preview.flushNow();
    thinking.flushNow();
    source.close();
    dispatch(disconnected());
  };
}
