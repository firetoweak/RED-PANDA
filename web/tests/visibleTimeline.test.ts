import { describe, expect, it } from "vitest";

import type { ConversationView } from "../src/api/contracts";
import {
  timelineTurns,
  turnIsSettled,
  turnNeedsSilentEnd,
  turnNeedsThinkingHint,
  turnReply,
  type TimelineTurn,
} from "../src/features/conversation/timelineTurns";
import { visibleTimeline } from "../src/features/conversation/visibleTimeline";

const session = {
  status: "waiting",
  waiting_for: ["external_fact"],
  pending_authorization_ids: [],
  should_wake: false,
  has_active_subagents: false,
  control_approval: null,
  control_message: null,
  auto_authorize: false,
  paused: false,
};

const conversation: ConversationView = {
  session_id: "s1",
  workspace_id: "workspace-1",
  revision: 3,
  items: [
    {
      kind: "user",
      message_id: "user-1",
      text: "hi",
      occurred_at: "2026-09-15T08:00:00+00:00",
      images: [], files: [],
    },
    {
      kind: "step",
      step_id: "step-1",
      output_id: "user-1",
      text: "journal",
      thinking: null,
      tools: [
        {
          command_id: "cmd-1",
          name: "read_file",
          status: "queued",
          error: null,
          arguments: { path: "a.py" },
        },
      ],
      occurred_at: "2026-09-15T08:00:01+00:00",
    },
  ],
  session,
};

describe("visibleTimeline", () => {
  it("does not repeat a final that already exists in the journal projection", () => {
    const visible = visibleTimeline(
      conversation,
      { "user-1": "journal" },
      { outputId: "user-1", text: "stream" },
      {},
    );
    expect(visible).toHaveLength(2);
    expect(visible[1]).toMatchObject({ kind: "step", text: "journal" });
  });

  it("shows preview only before the same output identity reaches the journal", () => {
    const empty: ConversationView = {
      ...conversation,
      revision: 1,
      items: [conversation.items[0]],
    };
    expect(
      visibleTimeline(empty, {}, { outputId: "user-1", text: "流" }, {})[1],
    ).toMatchObject({
      key: "output:user-1",
      kind: "step",
      text: "流",
      pending: true,
    });
    expect(
      visibleTimeline(
        empty,
        { "user-1": "最终" },
        null,
        {},
        {},
        null,
        "running",
      )[1],
    ).toMatchObject({
      key: "output:user-1",
      kind: "step",
      text: "最终",
      pending: true,
    });
  });

  it("drops orphan committed output after the session is idle", () => {
    const empty: ConversationView = {
      ...conversation,
      revision: 1,
      items: [conversation.items[0]],
    };
    const visible = visibleTimeline(
      empty,
      { "notification-1": '{"code":"SKILL_SOURCE_ERROR"}' },
      null,
      {},
      {},
      null,
      "idle",
    );
    expect(visible).toHaveLength(1);
    expect(visible[0]).toMatchObject({ kind: "user" });
  });

  it("mounts an empty pending step as soon as preview starts", () => {
    const empty: ConversationView = {
      ...conversation,
      revision: 1,
      items: [conversation.items[0]],
    };

    expect(
      visibleTimeline(empty, {}, { outputId: "user-1", text: "" }, {})[1],
    ).toMatchObject({
      key: "output:user-1",
      text: "",
      pending: true,
    });
  });

  it("overlays exact running activity onto the matching queued tool", () => {
    const visible = visibleTimeline(
      conversation,
      {},
      null,
      { "cmd-1": { commandId: "cmd-1", name: "read_file", status: "running" } },
    );
    expect(visible[1]).toMatchObject({
      kind: "step",
      tools: [{ commandId: "cmd-1", status: "running" }],
    });
  });

  it("keeps journal success and failure over live progress", () => {
    const visible = visibleTimeline(
      {
        ...conversation,
        items: [
          conversation.items[0],
          {
            ...conversation.items[1],
            tools: [
              {
                command_id: "cmd-1",
                name: "read_file",
                status: "succeeded",
                error: null,
              },
            ],
          },
        ],
      },
      {},
      null,
      { "cmd-1": { commandId: "cmd-1", name: "read_file", status: "running" } },
    );
    expect(visible[1]).toMatchObject({
      tools: [{ commandId: "cmd-1", status: "succeeded" }],
    });
  });

  it("does not let stale running activity override authorization state", () => {
    const visible = visibleTimeline(
      {
        ...conversation,
        items: [
          conversation.items[0],
          {
            ...conversation.items[1],
            tools: [
              {
                command_id: "cmd-1",
                name: "write_file",
                status: "awaiting_authorization",
                error: null,
              },
            ],
          },
        ],
      },
      {},
      null,
      { "cmd-1": { commandId: "cmd-1", name: "write_file", status: "running" } },
    );
    expect(visible[1]).toMatchObject({
      tools: [{ commandId: "cmd-1", status: "awaiting_authorization" }],
    });
  });

  it("lets exact running activity temporarily overlay an unknown journal card", () => {
    const visible = visibleTimeline(
      {
        ...conversation,
        items: [
          conversation.items[0],
          {
            ...conversation.items[1],
            tools: [
              {
                command_id: "cmd-1",
                name: "glob",
                status: "unknown",
                error: "执行中断，结果未知",
              },
            ],
          },
        ],
      },
      {},
      null,
      { "cmd-1": { commandId: "cmd-1", name: "glob", status: "running" } },
    );
    expect(visible[1]).toMatchObject({
      tools: [{ commandId: "cmd-1", status: "running", error: null }],
    });
  });

  it("streams thinking onto the matching output identity", () => {
    const empty: ConversationView = {
      ...conversation,
      revision: 1,
      items: [conversation.items[0]],
    };
    const visible = visibleTimeline(
      empty,
      {},
      { outputId: "user-1", text: "" },
      {},
      {},
      { outputId: "user-1", text: "先看目录" },
    );
    expect(visible[1]).toMatchObject({
      outputId: "user-1",
      thinking: "先看目录",
      thinkingPending: true,
    });
  });
});

describe("timelineTurns", () => {
  it("keeps tool steps in the process and exposes the final no-tool step", () => {
    const items = visibleTimeline(
      {
        ...conversation,
        items: [
          ...conversation.items,
          {
            kind: "step",
            step_id: "step-2",
            output_id: "out-1",
            text: "final",
            thinking: null,
            tools: [],
            occurred_at: "2026-09-15T08:00:02+00:00",
          },
        ],
      },
      {},
      null,
      {},
    );

    expect(timelineTurns(items)[0]).toMatchObject({
      process: [{ key: "output:user-1" }],
      active: null,
      final: { key: "output:out-1", text: "final" },
    });
  });

  it("keeps an unfinished preview outside the execution process", () => {
    const items = visibleTimeline(
      { ...conversation, revision: 1, items: [conversation.items[0]] },
      {},
      { outputId: "user-1", text: "typing" },
      {},
    );

    expect(timelineTurns(items)[0]).toMatchObject({
      process: [],
      active: { key: "output:user-1", pending: true },
      final: null,
    });
  });
});

describe("turnNeedsThinkingHint", () => {
  const user = {
    key: "user-1",
    kind: "user" as const,
    text: "hi",
    images: [], files: [],
  };

  it("shows thinking while the latest turn is running without text or tools", () => {
    expect(
      turnNeedsThinkingHint(
        {
          key: "user-1",
          user,
          process: [],
          active: null,
          final: null,
        },
        { running: true, latest: true },
      ),
    ).toBe(true);
  });

  it("shows thinking for an empty live preview", () => {
    expect(
      turnNeedsThinkingHint(
        {
          key: "user-1",
          user,
          process: [],
          active: {
            key: "output:user-1",
            kind: "step",
            outputId: "user-1",
            text: "",
            thinking: null,
            thinkingPending: false,
            pending: true,
            tools: [],
          },
          final: null,
        },
        { running: true, latest: true },
      ),
    ).toBe(true);
  });

  it("hides the hint once streamed thinking text exists", () => {
    expect(
      turnNeedsThinkingHint(
        {
          key: "user-1",
          user,
          process: [],
          active: {
            key: "output:user-1",
            kind: "step",
            outputId: "user-1",
            text: "",
            thinking: "先看目录",
            thinkingPending: true,
            pending: true,
            tools: [],
          },
          final: null,
        },
        { running: true, latest: true },
      ),
    ).toBe(false);
  });

  it("hides thinking once streamed text or a running tool appears", () => {
    expect(
      turnNeedsThinkingHint(
        {
          key: "user-1",
          user,
          process: [],
          active: {
            key: "output:user-1",
            kind: "step",
            outputId: "user-1",
            text: "正在写",
            thinking: null,
            thinkingPending: false,
            pending: true,
            tools: [],
          },
          final: null,
        },
        { running: true, latest: true },
      ),
    ).toBe(false);
    expect(
      turnNeedsThinkingHint(
        {
          key: "user-1",
          user,
          process: [
            {
              key: "output:user-1",
              kind: "step",
              outputId: "user-1",
              text: null,
              thinking: null,
              thinkingPending: false,
              pending: false,
              tools: [
                {
                  commandId: "cmd-1",
                  name: "read_file",
                  status: "running",
                  error: null,
                  arguments: {},
                },
              ],
            },
          ],
          active: null,
          final: null,
        },
        { running: true, latest: true },
      ),
    ).toBe(false);
  });

  it("does not leave thinking on an earlier turn", () => {
    expect(
      turnNeedsThinkingHint(
        {
          key: "user-1",
          user,
          process: [],
          active: null,
          final: null,
        },
        { running: true, latest: false },
      ),
    ).toBe(false);
  });

  it("hides thinking once the turn has settled", () => {
    expect(
      turnNeedsThinkingHint(
        {
          key: "user-1",
          user,
          process: [toolOnlyStep("succeeded")],
          active: null,
          final: null,
        },
        { running: false, latest: true, settled: true },
      ),
    ).toBe(false);
  });
});

describe("turnIsSettled", () => {
  const user = {
    key: "user-1",
    kind: "user" as const,
    text: "安装这个 MCP",
    images: [], files: [],
  };

  it("closes an idle tool-only turn after install or test", () => {
    const turn = toolOnlyTurn(user);
    expect(
      turnIsSettled(turn, {
        latest: true,
        running: false,
        awaitingControl: false,
      }),
    ).toBe(true);
    expect(turnNeedsSilentEnd(turn, true)).toBe(true);
    expect(turnReply(turn, true)).toBeNull();
  });

  it("stays open while the session is still running", () => {
    expect(
      turnIsSettled(toolOnlyTurn(user), {
        latest: true,
        running: true,
        awaitingControl: false,
      }),
    ).toBe(false);
  });

  it("stays open while control approval is pending", () => {
    expect(
      turnIsSettled(toolOnlyTurn(user), {
        latest: true,
        running: false,
        awaitingControl: true,
      }),
    ).toBe(false);
  });

  it("stays open while a tool is still running or awaiting authorization", () => {
    expect(
      turnIsSettled(
        {
          key: "user-1",
          user,
          process: [toolOnlyStep("running")],
          active: null,
          final: null,
        },
        { latest: true, running: false, awaitingControl: false },
      ),
    ).toBe(false);
    expect(
      turnIsSettled(
        {
          key: "user-1",
          user,
          process: [toolOnlyStep("awaiting_authorization")],
          active: null,
          final: null,
        },
        { latest: true, running: false, awaitingControl: false },
      ),
    ).toBe(false);
  });

  it("closes earlier turns even if the current session is running", () => {
    expect(
      turnIsSettled(toolOnlyTurn(user), {
        latest: false,
        running: true,
        awaitingControl: false,
      }),
    ).toBe(true);
  });

  it("does not invent a silent end when a final reply exists", () => {
    const turn: TimelineTurn = {
      key: "user-1",
      user,
      process: [toolOnlyStep("succeeded")],
      active: null,
      final: {
        key: "output:out-1",
        kind: "step",
        outputId: "out-1",
        text: "已经装好并测过了",
        thinking: null,
        thinkingPending: false,
        pending: false,
        tools: [],
      },
    };
    expect(turnIsSettled(turn, {
      latest: true,
      running: false,
      awaitingControl: false,
    })).toBe(true);
    expect(turnNeedsSilentEnd(turn, true)).toBe(false);
    expect(turnReply(turn, true)).toMatchObject({
      text: "已经装好并测过了",
      streaming: false,
    });
  });

  it("keeps leftover preview text after idle, without streaming", () => {
    const turn: TimelineTurn = {
      key: "user-1",
      user,
      process: [toolOnlyStep("succeeded")],
      active: {
        key: "output:user-1",
        kind: "step",
        outputId: "user-1",
        text: "先测一下",
        thinking: null,
        thinkingPending: false,
        pending: true,
        tools: [],
      },
      final: null,
    };
    expect(
      turnIsSettled(turn, {
        latest: true,
        running: false,
        awaitingControl: false,
      }),
    ).toBe(true);
    expect(turnReply(turn, true)).toMatchObject({
      text: "先测一下",
      streaming: false,
    });
    expect(turnNeedsSilentEnd(turn, true)).toBe(false);
  });
});

function toolOnlyTurn(user: TimelineTurn["user"]): TimelineTurn {
  return {
    key: "user-1",
    user,
    process: [toolOnlyStep("succeeded")],
    active: null,
    final: null,
  };
}

function toolOnlyStep(
  status: "queued" | "succeeded" | "running" | "awaiting_authorization",
): TimelineTurn["process"][number] {
  return {
    key: "output:user-1",
    kind: "step",
    outputId: "user-1",
    text: null,
    thinking: null,
    thinkingPending: false,
    pending: false,
    tools: [
      {
        commandId: "cmd-1",
        name: "test_mcp_server",
        status,
        error: null,
        arguments: { server_id: "demo" },
      },
    ],
  };
}
