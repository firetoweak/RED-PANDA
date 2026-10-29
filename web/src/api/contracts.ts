import { z } from "zod";

const memorySchema = z.object({
  version: z.number().int().nonnegative(),
  content: z.string(),
  updated_by: z.string().nullable(),
}).strict();
export type WorkingMemory = z.infer<typeof memorySchema>;
export const memoryConflictSchema = z.object({ detail: z.string(), current: memorySchema }).strict();
export const sharedMemorySchema = memorySchema.extend({
  content: z.string().refine((value) => Array.from(value).length <= 500),
});
export type SharedMemory = z.infer<typeof sharedMemorySchema>;
export const sharedMemoryConflictSchema = z.object({ detail: z.string(), current: sharedMemorySchema }).strict();
export const personalMemorySchema = memorySchema.extend({
  content: z.string().refine((value) => Array.from(value).length <= 200),
});
export type PersonalMemory = z.infer<typeof personalMemorySchema>;
export const personalMemoryConflictSchema = z.object({ detail: z.string(), current: personalMemorySchema }).strict();

const controlApprovalSchema = z
  .object({
    request_id: z.string().min(1),
    summary: z.string().min(1),
    risk: z.string().min(1),
  })
  .strict();

const pendingAuthorizationSchema = z
  .object({
    command_id: z.string().min(1),
    name: z.string().min(1),
    arguments: z.record(z.string(), z.unknown()),
  })
  .strict();

export type PendingAuthorization = z.infer<typeof pendingAuthorizationSchema>;

export const inboxItemSchema = z.discriminatedUnion("kind", [
  z.object({
    kind: z.literal("authorization"),
    command_session_id: z.string().min(1),
    session_id: z.string().min(1),
    workspace_id: z.string().min(1).nullable(),
    label: z.string().min(1),
    command_id: z.string().min(1),
    name: z.string().min(1),
    arguments: z.record(z.string(), z.unknown()),
  }).strict(),
  z.object({
    kind: z.literal("control"),
    session_id: z.string().min(1),
    workspace_id: z.string().min(1).nullable(),
    label: z.string().min(1),
    request_id: z.string().min(1),
    summary: z.string().min(1),
    risk: z.string().min(1),
  }).strict(),
  z.object({
    kind: z.literal("limit"),
    session_id: z.string().min(1),
    workspace_id: z.string().min(1).nullable(),
    label: z.string().min(1),
    event_id: z.string().min(1),
    recipient: z.string().min(1),
    text: z.string().min(1),
  }).strict(),
  z.object({
    kind: z.literal("absence"),
    session_id: z.string().min(1),
    workspace_id: z.string().min(1).nullable(),
    label: z.string().min(1),
    phase: z.enum(["asked", "paused"]),
    summary: z.string().min(1),
  }).strict(),
]);

export type InboxItem = z.infer<typeof inboxItemSchema>;

const sessionViewSchema = z
  .object({
    status: z.string().min(1),
    waiting_for: z.array(z.string().min(1)),
    pending_authorization_ids: z.array(z.string().min(1)),
    pending_authorization_commands: z.array(pendingAuthorizationSchema),
    should_wake: z.boolean(),
    has_open_subagents: z.boolean(),
    control_approval: controlApprovalSchema.nullable(),
    control_message: z.string().nullable(),
    auto_authorize: z.boolean(),
    last_error: z.string().nullable(),
  })
  .strict();

export const agentProjectSchema = z
  .object({
    name: z.string().min(1),
    workspace_id: z.string().min(1),
    directory: z.string().min(1),
  })
  .strict();

export const agentRosterEntrySchema = z
  .object({
    session_id: z.string().min(1),
    projects: agentProjectSchema.array(),
    name: z.string().min(1),
    activity: z.enum(["running", "idle"]),
    status: z.enum(["working", "authorization", "waiting", "idle"]),
  })
  .strict();

export type AgentRosterEntry = z.infer<typeof agentRosterEntrySchema>;

export const externalConnectionSchema = z.object({
  connection_id: z.string().regex(/^[0-9a-f]{32}$/),
  name: z.string(),
}).strict();
export type ExternalConnection = z.infer<typeof externalConnectionSchema>;
export const createdExternalConnectionSchema = z.object({
  connection: externalConnectionSchema,
  token: z.string().min(1),
  url: z.url(),
}).strict();
export type CreatedExternalConnection = z.infer<typeof createdExternalConnectionSchema>;

export const externalSessionSchema = z.object({
  identity: z.string().regex(/^[0-9a-f]{32}$/),
  name: z.string().min(1),
  created_at: z.string().datetime({ offset: true }),
  last_activity_at: z.string().datetime({ offset: true }),
  active: z.boolean(),
  peers: z.array(z.object({ name: z.string().min(1), session_id: z.string().min(1), deleted: z.boolean() }).strict()),
}).strict();
export type ExternalSessionSummary = z.infer<typeof externalSessionSchema>;
export const externalConversationSchema = z.object({
  session: externalSessionSchema,
  codex: z.object({
    thread_id: z.string().nullable(),
    executable: z.string().min(1),
    status: z.enum(["disabled", "ready", "pending", "queued", "failed", "unknown"]),
    detail: z.string().nullable(),
    attempted_at: z.string().datetime({ offset: true }).nullable(),
  }).strict(),
  messages: z.array(z.object({
    message_id: z.string().min(1),
    sequence: z.number().int().positive(),
    direction: z.enum(["received", "sent"]),
    text: z.string().min(1),
    reply_to: z.string().nullable(),
    bot_name: z.string().min(1),
    bot_session_id: z.string().min(1),
    occurred_at: z.string().datetime({ offset: true }),
  }).strict()),
}).strict();
export type ExternalConversation = z.infer<typeof externalConversationSchema>;
export type ExternalConversationMessage = ExternalConversation["messages"][number];
export const externalSessionsChangedEventSchema = z.object({}).strict();

export const avatarSelectionSchema = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("generated"), seed: z.string().min(1) }).strict(),
  z.object({ kind: z.literal("image"), attachment_id: z.string().regex(/^sha256:[0-9a-f]{64}$/) }).strict(),
]);
export const avatarsSchema = z.record(z.string(), avatarSelectionSchema);
export const avatarsChangedEventSchema = z.object({}).strict();
export type AvatarSelection = z.infer<typeof avatarSelectionSchema>;
export type Avatars = z.infer<typeof avatarsSchema>;

export const workspaceSchema = z
  .object({
    workspace_id: z.string().min(1),
    name: z.string().min(1),
    task_root: z.string().min(1),
    full_access: z.boolean(),
    created_at: z.string().min(1),
  })
  .strict();

export type Workspace = z.infer<typeof workspaceSchema>;

export const groupSummarySchema = z
  .object({
    group_id: z.string().min(1),
    name: z.string().min(1),
    members: z.array(z.string().min(1)),
  })
  .strict();

export type GroupSummary = z.infer<typeof groupSummarySchema>;

export const groupMessageSchema = z
  .object({
    message_id: z.string().min(1),
    sender: z.string().min(1).nullable(),
    text: z.string().min(1),
    mentioned_agents: z.array(z.string().min(1)),
    reply_to: z.string().min(1).nullable(),
    occurred_at: z.string().min(1),
  })
  .strict();

export type GroupMessage = z.infer<typeof groupMessageSchema>;

export const groupViewSchema = groupSummarySchema
  .extend({
    dissolved: z.boolean(),
    calling: z.array(z.string().min(1)),
    messages: z.array(groupMessageSchema),
  })
  .strict();

export type GroupView = z.infer<typeof groupViewSchema>;

export const directorySelectionSchema = z
  .object({
    directory: z
      .object({ path: z.string().min(1), name: z.string().min(1) })
      .strict()
      .nullable(),
  })
  .strict();

export type DirectorySelection = z.infer<typeof directorySelectionSchema>;


export const routineSchema = z
  .object({
    routine_id: z.string().min(1),
    name: z.string().min(1),
    intent: z.string().min(1),
    schedule: z.string().min(1),
    state: z.enum(["active", "paused", "paused_by_user", "paused_unwatched"]),
    next_run: z.string().datetime({ offset: true }).nullable(),
    last_run: z.string().datetime({ offset: true }).nullable(),
  })
  .strict();

export type Routine = z.infer<typeof routineSchema>;

export const routineFireSchema = z
  .object({
    fire_id: z.string().min(1),
    planned_at: z.string().datetime({ offset: true }),
    fired_at: z.string().datetime({ offset: true }),
    delivered_at: z.string().datetime({ offset: true }).nullable(),
    output_id: z.string().min(1).nullable(),
  })
  .strict();

export type RoutineFire = z.infer<typeof routineFireSchema>;

export const toolStatusSchema = z.enum([
  "queued",
  "running",
  "succeeded",
  "failed",
  "unknown",
  "awaiting_authorization",
  "rejected",
]);

const toolItemSchema = z
  .object({
    command_id: z.string().min(1),
    name: z.string().min(1),
    status: toolStatusSchema,
    error: z.string().min(1).nullable(),
    arguments: z.record(z.string(), z.unknown()),
  })
  .strict();

export const workPlanSchema = z.object({
  objective: z.string().min(1),
  steps: z.array(z.object({
    text: z.string().min(1),
    status: z.enum(["pending", "in_progress", "completed"]),
  }).strict()).min(1),
  note: z.string().min(1).nullable(),
}).strict();

export type WorkPlan = z.infer<typeof workPlanSchema>;

export const conversationViewSchema = z
  .object({
    deleted: z.boolean(),
    agent_name: z.string().min(1).nullable(),
    session_id: z.string().min(1),
    workspace_id: z.string().min(1).nullable(),
    revision: z.number().int().nonnegative(),
    items: z.array(
      z.discriminatedUnion("kind", [
        z.object({
          kind: z.literal("external_message"),
          message_id: z.string().min(1),
          direction: z.enum(["sent", "received"]),
          peer: z.string().min(1),
          identity: z.string().min(1),
          text: z.string().min(1),
          reply_to: z.string().nullable(),
          occurred_at: z.string().datetime({ offset: true }),
        }).strict(),
        z.object({
          kind: z.literal("private_message"),
          message_id: z.string().min(1),
          thread_id: z.string().min(1),
          direction: z.enum(["sent", "received"]),
          peer: z.string().min(1),
          text: z.string().min(1),
          reply_to: z.string().nullable(),
          occurred_at: z.string().datetime({ offset: true }),
        }).strict(),
        z
          .object({
            kind: z.literal("user"),
            message_id: z.string().min(1),
            text: z.string().min(1),
            occurred_at: z.string().datetime({ offset: true }),
            images: z.array(z.string().regex(/^sha256:[0-9a-f]{64}$/)),
            files: z.array(z.object({
              attachment_id: z.string().regex(/^file:[0-9a-f]{32}$/),
              name: z.string().min(1),
              size: z.number().int().nonnegative(),
            }).strict()),
          })
          .strict(),
        z
          .object({
            kind: z.literal("step"),
            step_id: z.string().min(1),
            output_id: z.string().min(1),
            text: z.string().min(1).nullable(),
            thinking: z.string().min(1).nullable(),
            tools: z.array(toolItemSchema),
            occurred_at: z.string().datetime({ offset: true }),
            files: z.array(z.object({
              attachment_id: z.string().regex(/^file:[0-9a-f]{32}$/),
              name: z.string().min(1),
              size: z.number().int().nonnegative(),
            }).strict()).default([]),
          })
          .strict(),
      ]),
    ),
    session: sessionViewSchema,
    compact_count: z.number().int().nonnegative(),
    context_input_tokens: z.number().int().nonnegative().nullable(),
    compact_phase: z.enum(["running", "ready", "failed"]).nullable(),
    waiting_until: z.string().datetime({ offset: true }).nullable(),
    work_plan: workPlanSchema.nullable(),
    work_plan_updates: z.array(z.object({
      step_id: z.string().min(1),
      plan: workPlanSchema.nullable(),
    }).strict()),
    subagent_terminals: z.array(z.object({
      command_id: z.string().min(1),
      child_session_id: z.string().min(1),
      failure: z.string().nullable(),
      reason: z.string().nullable(),
    }).strict()).optional(),
  })
  .strict();

export const connectedEventSchema = z
  .object({ connection_id: z.string().min(1) })
  .strict();

export const sessionActivityEventSchema = z
  .object({
    session_id: z.string().min(1),
    activity: z.enum(["running", "idle"]),
  })
  .strict();

export const scheduleChangedEventSchema = z
  .object({ session_id: z.string().min(1) })
  .strict();

export const groupChangedEventSchema = z
  .object({ group_id: z.string().min(1) })
  .strict();

export const sessionFailedEventSchema = z
  .object({
    session_id: z.string().min(1),
    message: z.string().min(1),
  })
  .strict();

export const previewStartedEventSchema = z
  .object({
    session_id: z.string().min(1),
    output_id: z.string().min(1),
  })
  .strict();

export const previewDeltaEventSchema = z
  .object({
    session_id: z.string().min(1),
    output_id: z.string().min(1),
    text: z.string().min(1),
  })
  .strict();

export const previewAbortedEventSchema = z
  .object({
    session_id: z.string().min(1),
    output_id: z.string().min(1),
  })
  .strict();

export const thinkingStartedEventSchema = previewStartedEventSchema;
export const thinkingDeltaEventSchema = previewDeltaEventSchema;
export const thinkingFinishedEventSchema = previewAbortedEventSchema;

export const outputFinalEventSchema = z
  .object({
    session_id: z.string().min(1),
    output_id: z.string().min(1),
    text: z.string().min(1),
  })
  .strict();

export const toolProgressEventSchema = z
  .object({
    session_id: z.string().min(1),
    command_id: z.string().min(1),
    name: z.string().min(1),
    status: z.enum(["running", "settled"]),
  })
  .strict();

export const authorizationRequiredEventSchema = z
  .object({
    session_id: z.string().min(1),
    command_id: z.string().min(1),
    name: z.string().min(1),
    arguments: z.record(z.string(), z.unknown()),
  })
  .strict();

export const modelProfileSchema = z.object({
  model: z.string().min(1),
  compact_threshold_tokens: z.number().int().positive(),
  reasoning_effort: z.enum(["low", "medium", "high"]).optional(),
}).strict();

export const modelConfigSchema = z.object({
  model: z.object({
    default: z.string().nullable(),
    candidates: modelProfileSchema.array(),
  }).strict(),
}).strict();

export const autoAuthorizeSchema = z.object({ enabled: z.boolean() }).strict();
export const autoAuthorizeChangedEventSchema = z.object({}).strict();

export const modelSettingsSchema = z.object({
  config: modelConfigSchema,
  connections_path: z.string().min(1),
  providers: z.object({
    provider: z.string().min(1),
    configured: z.boolean(),
    missing: z.string().nullable(),
    local: z.boolean(),
  }).strict().array(),
}).strict();

export const codexModelsSchema = z.object({
  slug: z.string().min(1),
  display_name: z.string().min(1),
  context_window: z.number().int().positive().nullable(),
  default_reasoning_level: z.string().nullable(),
  supported_reasoning_levels: z.object({ effort: z.string().min(1) }).strict().array(),
}).strict().array();

export const sessionModelSchema = z.object({
  selected: modelProfileSchema.nullable(),
  effective: modelProfileSchema.nullable(),
  pending: z.boolean(),
}).strict();

export const modelTestResultSchema = z.object({
  model: z.string().min(1),
  ok: z.boolean(),
  message: z.string().min(1),
  elapsed_ms: z.number().int().nonnegative(),
}).strict();

export const contextUsageEventSchema = z
  .object({
    session_id: z.string().min(1),
    used: z.number().int().nonnegative(),
    compact_threshold_tokens: z.number().int().positive(),
  })
  .strict();

export const conversationStatusEventSchema = z
  .object({
    session_id: z.string().min(1),
    compact_count: z.number().int().nonnegative(),
    compact_phase: z.enum(["running", "ready", "failed"]).nullable(),
  })
  .strict();

export const subagentSummarySchema = z.object({
  command_id: z.string().min(1),
  child_session_id: z.string().min(1),
  task: z.string().min(1),
  status: z.enum(["creating", "running", "waiting", "closed", "failed"]),
}).strict();
export type SubagentSummary = z.infer<typeof subagentSummarySchema>;

export const subagentObservationSchema = z.object({
  session_id: z.string().min(1),
  task: z.string().min(1),
  activity: z.enum(["running", "idle"]),
  conversation: conversationViewSchema.nullable(),
  result: z.object({
    failure: z.string().nullable(), reason: z.string().nullable(),
  }).strict().nullable(),
}).strict();

export type SubagentObservation = z.infer<typeof subagentObservationSchema>;
export type ConversationView = z.infer<typeof conversationViewSchema>;
export type ConversationItem = ConversationView["items"][number];
export type ToolStatus = z.infer<typeof toolStatusSchema>;
export type ModelProfile = z.infer<typeof modelProfileSchema>;
export type ModelConfig = z.infer<typeof modelConfigSchema>;
export type ModelSettings = z.infer<typeof modelSettingsSchema>;
export type CodexModels = z.infer<typeof codexModelsSchema>;
export type ModelTestResult = z.infer<typeof modelTestResultSchema>;
export type SessionModel = z.infer<typeof sessionModelSchema>;

export const backgroundCommandSchema = z.object({
  command_id: z.string(), pid: z.number().int().nullable(), command: z.string(), cwd: z.string(),
  started_at: z.string(), state: z.enum(["running", "completed", "stopped", "unknown"]),
  exit_code: z.number().int().nullable(), duration_ms: z.number().int(),
});
export type BackgroundCommand = z.infer<typeof backgroundCommandSchema>;
