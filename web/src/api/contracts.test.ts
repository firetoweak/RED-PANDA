import { describe, expect, it } from "vitest";

import {
  contextUsageEventSchema,
  conversationStatusEventSchema,
  conversationViewSchema,
  outputFinalEventSchema,
  modelProfileSchema,
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
      waiting_until: null, workspace_version: null, work_plan: null, work_plan_updates: [], context_input_tokens: null,
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
      waiting_until: null, workspace_version: null, work_plan: null, work_plan_updates: [], context_input_tokens: null,
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
      waiting_until: null, workspace_version: null, work_plan: null, work_plan_updates: [], context_input_tokens: null,
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
      waiting_until: null, workspace_version: null, work_plan: null, work_plan_updates: [], context_input_tokens: null,
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
      waiting_until: null, workspace_version: null, work_plan: null, work_plan_updates: [], context_input_tokens: null,
    });
    expect(parsed.items[0]).toMatchObject({
      kind: "step",
      tools: [{ status: "queued" }],
    });
  });
});

describe("effect review on a step", () => {
  const envelope = (step: Record<string, unknown>) => ({
    session_id: "session-1",
    workspace_id: "workspace-1",
    revision: 1,
    items: [step],
    session,
    compact_count: 0,
    compact_phase: null,
    waiting_until: null,
    workspace_version: null,
    work_plan: null,
    work_plan_updates: [],
    context_input_tokens: null,
  });

  const step = {
    kind: "step",
    step_id: "step-event",
    output_id: "user-event",
    text: "启动已经更快。",
    thinking: null,
    tools: [],
    occurred_at: "2026-09-15T08:00:01+00:00",
    rewindable: true,
  };

  it("accepts a structured effect review and still accepts a step without one", () => {
    const review = {
      conclusion: "启动耗时从 1.8s 降到 0.4s。",
      metrics: [{ label: "启动耗时", before: "1.8", after: "0.4", unit: "s" }],
      changes: [{ path: "web/src/main.tsx", reason: "去掉重复初始化" }],
      actions: [
        { kind: "authorize", label: "允许检查", command_id: "cmd-1", approved: true },
        { kind: "restore", label: "从这一步之后重开", step_id: "step-event" },
        { kind: "todo", label: "接受并发布", note: "还没有对应的 Host 接口。" },
      ],
    };
    expect(conversationViewSchema.parse(envelope({ ...step, effect_review: review })).items[0])
      .toMatchObject({ effect_review: review });
    expect(conversationViewSchema.parse(envelope(step)).items[0])
      .toMatchObject({ kind: "step", text: "启动已经更快。" });
    expect(conversationViewSchema.parse(envelope({ ...step, effect_review: null })).items[0])
      .toMatchObject({ effect_review: null });
  });

  it("rejects an action that is not an existing host call or an explicit todo", () => {
    expect(() => conversationViewSchema.parse(envelope({
      ...step,
      effect_review: {
        conclusion: "完成",
        metrics: [],
        changes: [],
        actions: [{ kind: "tool", label: "直接改文件", name: "write_file" }],
      },
    }))).toThrow();
  });
});

describe("modelProfileSchema", () => {
  it("requires a model name and a positive compact threshold", () => {
    expect(
      modelProfileSchema.parse({ model: "assistant", compact_threshold_tokens: 200000 }),
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
