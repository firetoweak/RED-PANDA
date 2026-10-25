import { describe, expect, it } from "vitest";

import {
  conversationViewSchema,
  outputFinalEventSchema,
  modelProfileSchema,
  toolProgressEventSchema,
} from "../src/api/contracts";

const attachmentId = `sha256:${"a".repeat(64)}`;
const user = {
  kind: "user", message_id: "user-event", text: "hello",
  occurred_at: "2026-09-15T08:00:00+00:00", images: [attachmentId], files: [],
};
const step = {
  kind: "step", step_id: "step-event", output_id: "user-event",
  text: "world", thinking: "先确认目标", occurred_at: "2026-09-15T08:00:01+00:00",
  tools: [{ command_id: "cmd-1", name: "read_file", status: "succeeded",
    error: null, arguments: { path: "README.md" } }],
};

function conversation(items: unknown[] = [user, step]) {
  return {
    session_id: "session-1", workspace_id: "workspace-1", revision: 3,
    items, deleted: false, agent_name: null,
    compact_count: 0, compact_phase: null, waiting_until: null,
    work_plan: null, work_plan_updates: [], context_input_tokens: null,
    session: {
      status: "waiting", waiting_for: ["external_fact"],
      pending_authorization_ids: [], pending_authorization_commands: [],
      should_wake: false, has_open_subagents: false,
      control_approval: null, control_message: null, auto_authorize: false, last_error: null,
    },
  };
}

describe("conversationViewSchema", () => {
  it("keeps step identity, output identity and nested command identity separate", () => {
    const parsed = conversationViewSchema.parse(conversation());
    expect(parsed.items[0]).toMatchObject({ kind: "user", images: [attachmentId], files: [] });
    expect(parsed.items[1]).toMatchObject({
      kind: "step", step_id: "step-event", output_id: "user-event",
      text: "world", thinking: "先确认目标",
      tools: [{ command_id: "cmd-1", status: "succeeded" }],
    });
  });

  it("rejects a user item that carries an output identity", () => {
    const valid = conversation([user]);
    expect(conversationViewSchema.safeParse(valid).success).toBe(true);
    const invalid = conversationViewSchema.safeParse({
      ...valid, items: [{ ...user, output_id: "wrong" }],
    });
    expect(invalid.error?.issues).toEqual([
      expect.objectContaining({
        code: "unrecognized_keys", keys: ["output_id"], path: ["items", 0],
      }),
    ]);
  });
});

describe("outputFinalEventSchema", () => {
  it("requires a session and the same output identity used by preview", () => {
    const valid = { session_id: "session-1", output_id: "user-event", text: "world" };
    expect(outputFinalEventSchema.parse(valid)).toEqual(valid);
    for (const field of ["session_id", "output_id"] as const) {
      const invalid: Partial<typeof valid> = { ...valid };
      delete invalid[field];
      expect(outputFinalEventSchema.safeParse(invalid).success).toBe(false);
    }
  });
});

describe("toolProgressEventSchema", () => {
  it("identifies transient activity by command_id without claiming a terminal", () => {
    const parsed = toolProgressEventSchema.parse({
      session_id: "session-1", command_id: "cmd-1", name: "read_file", status: "running",
    });
    expect(parsed.command_id).toBe("cmd-1");
    expect(toolProgressEventSchema.parse({
      session_id: "session-1", command_id: "cmd-1", name: "read_file", status: "settled",
    }).status).toBe("settled");
  });

  it("accepts unknown as a journal tool status", () => {
    const parsed = conversationViewSchema.parse(conversation([{
      ...step, tools: [{ ...step.tools[0], status: "unknown", error: "执行中断，结果未知" }],
    }]));
    expect(parsed.items[0]).toMatchObject({
      tools: [{ command_id: "cmd-1", status: "unknown", error: "执行中断，结果未知" }],
    });
  });

  it("accepts queued before an execution attempt exists", () => {
    const parsed = conversationViewSchema.parse(conversation([{
      ...step, tools: [{ ...step.tools[0], status: "queued" }],
    }]));
    expect(parsed.items[0]).toMatchObject({ tools: [{ status: "queued" }] });
  });
});

describe("modelProfileSchema", () => {
  it("requires a model name and a positive compact threshold", () => {
    const valid = { model: "assistant", compact_threshold_tokens: 200000 };
    expect(modelProfileSchema.parse(valid)).toEqual(valid);
    expect(modelProfileSchema.safeParse({ ...valid, model: "" }).success).toBe(false);
    expect(modelProfileSchema.safeParse({ compact_threshold_tokens: 200000 }).success).toBe(false);
    for (const threshold of [0, -1, 1.5, "1", true]) {
      expect(modelProfileSchema.safeParse({ ...valid, compact_threshold_tokens: threshold }).success).toBe(false);
    }
  });
});
