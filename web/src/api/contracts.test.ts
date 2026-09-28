import { describe, expect, it } from "vitest";

import {
  contextUsageEventSchema,
  conversationStatusEventSchema,
  conversationViewSchema,
  outputFinalEventSchema,
  runtimeStatusSchema,
  sessionFailedEventSchema,
  toolProgressEventSchema,
} from "./contracts";

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

describe("conversationViewSchema", () => {
  it("keeps step identity, output identity and nested command identity separate", () => {
    const parsed = conversationViewSchema.parse({
      session_id: "session-1",
      workspace_id: "workspace-1",
      revision: 3,
      items: [
        {
          kind: "user",
          message_id: "user-event",
          text: "hello",
          occurred_at: "2026-09-15T08:00:00+00:00",
          images: [], files: [],
        },
        {
          kind: "step",
          step_id: "step-event",
          output_id: "user-event",
          text: "world",
          thinking: null,
          tools: [
            {
              command_id: "cmd-1",
              name: "read_file",
              status: "succeeded",
              error: null,
              arguments: { path: "README.md" },
            },
          ],
          occurred_at: "2026-09-15T08:00:01+00:00",
          rewindable: false,
        },
      ],
      session,
      compact_count: 0,
      compact_phase: null,
      waiting_until: null, workspace_version: null,
    });

    expect(parsed.items[1]).toMatchObject({
      kind: "step",
      step_id: "step-event",
      output_id: "user-event",
    });
    expect(parsed.items[1]).toMatchObject({
      kind: "step",
      tools: [{
      command_id: "cmd-1",
      status: "succeeded",
      }],
    });
  });

  it("rejects a user item that carries an output identity", () => {
    expect(() =>
      conversationViewSchema.parse({
        session_id: "session-1",
        workspace_id: "workspace-1",
        revision: 1,
        items: [
          {
            kind: "user",
            message_id: "user-event",
            output_id: "wrong",
            text: "hello",
            occurred_at: "2026-09-15T08:00:00+00:00",
            images: [], files: [],
          },
        ],
        session,
      }),
    ).toThrow();
  });

  it("carries image attachment ids on a user item", () => {
    const attachmentId = `sha256:${"a".repeat(64)}`;
    const parsed = conversationViewSchema.parse({
      session_id: "session-1",
      workspace_id: "workspace-1",
      revision: 1,
      items: [
        {
          kind: "user",
          message_id: "user-event",
          text: "[Image #1]",
          occurred_at: "2026-09-15T08:00:00+00:00",
          images: [attachmentId], files: [],
        },
      ],
      session,
      compact_count: 0,
      compact_phase: null,
      waiting_until: null, workspace_version: null,
    });
    expect(parsed.items[0]).toMatchObject({
      kind: "user",
      images: [attachmentId], files: [],
    });
  });
});

describe("conversation thinking field", () => {
  it("accepts reasoning text on a step without treating it as reply text", () => {
    const parsed = conversationViewSchema.parse({
      session_id: "session-1",
      workspace_id: "workspace-1",
      revision: 1,
      items: [
        {
          kind: "step",
          step_id: "step-event",
          output_id: "user-event",
          text: "world",
          thinking: "先确认目标",
          tools: [],
          occurred_at: "2026-09-15T08:00:01+00:00",
          rewindable: false,
        },
      ],
      session,
      compact_count: 0,
      compact_phase: null,
      waiting_until: null, workspace_version: null,
    });
    expect(parsed.items[0]).toMatchObject({
      text: "world",
      thinking: "先确认目标",
    });
  });
});

describe("outputFinalEventSchema", () => {
  it("requires a session and the same output identity used by preview", () => {
    const parsed = outputFinalEventSchema.parse({
      session_id: "session-1",
      output_id: "user-event",
      text: "world",
    });
    expect(parsed.output_id).toBe("user-event");
  });
});

describe("toolProgressEventSchema", () => {
  it("identifies transient activity by command_id without claiming a terminal", () => {
    const parsed = toolProgressEventSchema.parse({
      session_id: "session-1",
      command_id: "cmd-1",
      name: "read_file",
      status: "running",
    });
    expect(parsed.command_id).toBe("cmd-1");

    expect(
      toolProgressEventSchema.parse({
        session_id: "session-1",
        command_id: "cmd-1",
        name: "read_file",
        status: "settled",
      }).status,
    ).toBe("settled");
  });

  it("accepts unknown as a journal tool status", () => {
    const parsed = conversationViewSchema.parse({
      session_id: "session-1",
      workspace_id: "workspace-1",
      revision: 1,
      items: [
        {
          kind: "step",
          step_id: "step-event",
          output_id: "user-event",
          text: null,
          thinking: null,
          tools: [
            {
              command_id: "cmd-1",
              name: "glob",
              status: "unknown",
              error: "执行中断，结果未知",
              arguments: { pattern: "*.py" },
            },
          ],
          occurred_at: "2026-09-15T08:00:01+00:00",
          rewindable: false,
        },
      ],
      session,
      compact_count: 0,
      compact_phase: null,
      waiting_until: null, workspace_version: null,
    });
    expect(parsed.items[0]).toMatchObject({
      tools: [{ command_id: "cmd-1", status: "unknown" }],
    });
  });

  it("accepts queued before an execution attempt exists", () => {
    const parsed = conversationViewSchema.parse({
      session_id: "session-1",
      workspace_id: "workspace-1",
      revision: 1,
      items: [
        {
          kind: "step",
          step_id: "step-event",
          output_id: "user-event",
          text: null,
          thinking: null,
          tools: [
            {
              command_id: "cmd-1",
              name: "read_file",
              status: "queued",
              error: null,
              arguments: {},
            },
          ],
          occurred_at: "2026-09-15T08:00:01+00:00",
          rewindable: false,
        },
      ],
      session,
      compact_count: 0,
      compact_phase: null,
      waiting_until: null, workspace_version: null,
    });
    expect(parsed.items[0]).toMatchObject({
      kind: "step",
      tools: [{ status: "queued" }],
    });
  });
});

describe("runtimeStatusSchema", () => {
  it("requires a model name and a positive compact threshold", () => {
    expect(
      runtimeStatusSchema.parse({ model: "assistant", compact_threshold_tokens: 200000 }),
    ).toEqual({
      model: "assistant",
      compact_threshold_tokens: 200000,
    });
  });
});

describe("sessionFailedEventSchema", () => {
  it("identifies a recognised run failure by session", () => {
    expect(
      sessionFailedEventSchema.parse({
        session_id: "session-1",
        message: "运行失败：模型服务暂时不可用",
      }),
    ).toEqual({
      session_id: "session-1",
      message: "运行失败：模型服务暂时不可用",
    });
  });
});

describe("conversationStatusEventSchema", () => {
  it("identifies compact status by session", () => {
    expect(
      conversationStatusEventSchema.parse({
        session_id: "session-1",
        compact_count: 1,
        compact_phase: "running",
      }),
    ).toEqual({
      session_id: "session-1",
      compact_count: 1,
      compact_phase: "running",
    });
  });
});

describe("contextUsageEventSchema", () => {
  it("identifies usage by session", () => {
    expect(
      contextUsageEventSchema.parse({
        session_id: "session-1",
        used: 1200,
        compact_threshold_tokens: 200000,
      }),
    ).toEqual({
      session_id: "session-1",
      used: 1200,
      compact_threshold_tokens: 200000,
    });
  });
});
