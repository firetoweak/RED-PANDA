import { describe, expect, it } from "vitest";

import type { ConversationView } from "./contracts";
import { truncateAfterUserMessage } from "./truncateAfterUserMessage";

const conversation: ConversationView = {
  session_id: "s1",
  workspace_id: "workspace-1",
  revision: 3,
  items: [
    {
      kind: "user",
      message_id: "user-1",
      text: "first",
      occurred_at: "2026-09-15T08:00:00+00:00",
      images: [],
    },
    {
      kind: "step",
      step_id: "step-1",
      output_id: "user-1",
      text: "reply",
      thinking: null,
      tools: [],
      occurred_at: "2026-09-15T08:00:01+00:00",
      rewindable: false,
    },
    {
      kind: "user",
      message_id: "user-2",
      text: "second",
      occurred_at: "2026-09-15T08:00:02+00:00",
      images: [],
    },
    {
      kind: "step",
      step_id: "step-2",
      output_id: "user-2",
      text: "later",
      thinking: null,
      tools: [],
      occurred_at: "2026-09-15T08:00:03+00:00",
      rewindable: false,
    },
  ],
  session: {
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
  },
  compact_count: 0,
  compact_phase: null,
  waiting_until: null, workspace_version: null,
};

describe("truncateAfterUserMessage", () => {
  it("keeps the edited user message and drops everything after it", () => {
    const truncated = truncateAfterUserMessage(conversation, "user-1", "edited");
    expect(truncated.items).toEqual([
      {
        kind: "user",
        message_id: "user-1",
        text: "edited",
        occurred_at: "2026-09-15T08:00:00+00:00",
        images: [],
      },
    ]);
  });
});
