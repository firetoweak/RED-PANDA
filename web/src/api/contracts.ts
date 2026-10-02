import { z } from "zod";

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

const sessionViewSchema = z
  .object({
    status: z.string().min(1),
    waiting_for: z.array(z.string().min(1)),
    pending_authorization_ids: z.array(z.string().min(1)),
    pending_authorization_commands: z.array(pendingAuthorizationSchema),
    should_wake: z.boolean(),
    has_active_subagents: z.boolean(),
    control_approval: controlApprovalSchema.nullable(),
    control_message: z.string().nullable(),
    auto_authorize: z.boolean(),
    paused: z.boolean(),
  })
  .strict();

export const sessionSummarySchema = z
  .object({
    session_id: z.string().min(1),
    workspace_id: z.string().min(1),
    title: z.string().min(1),
    updated_at: z.string().datetime({ offset: true }).nullable(),
    activity: z.enum(["running", "idle"]),
  })
  .strict();

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

export const directorySelectionSchema = z
  .object({
    directory: z
      .object({ path: z.string().min(1), name: z.string().min(1) })
      .strict()
      .nullable(),
  })
  .strict();

export type DirectorySelection = z.infer<typeof directorySelectionSchema>;

export const fileSelectionSchema = z
  .object({
    file: z
      .object({ path: z.string().min(1), name: z.string().min(1) })
      .strict()
      .nullable(),
  })
  .strict();

export type FileSelection = z.infer<typeof fileSelectionSchema>;

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
    session_id: z.string().min(1),
    workspace_id: z.string().min(1).nullable(),
    revision: z.number().int().nonnegative(),
    items: z.array(
      z.discriminatedUnion("kind", [
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
            rewindable: z.boolean(),
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
    workspace_version: z.object({
      workspace_id: z.string().min(1),
      step_id: z.string().min(1).nullable(),
      version: z.string().regex(/^[0-9a-f]{40}$/).nullable(),
      error: z.string().min(1).nullable(),
    }).strict().nullable(),
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
}).strict();

export const modelConfigSchema = z.object({
  model: z.object({
    default: z.string().nullable(),
    candidates: modelProfileSchema.array(),
  }).strict(),
}).strict();

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

export type SessionSummary = z.infer<typeof sessionSummarySchema>;
export type ConversationView = z.infer<typeof conversationViewSchema>;
export type ConversationItem = ConversationView["items"][number];
export type ToolStatus = z.infer<typeof toolStatusSchema>;
export type ModelProfile = z.infer<typeof modelProfileSchema>;
export type ModelConfig = z.infer<typeof modelConfigSchema>;
export type ModelSettings = z.infer<typeof modelSettingsSchema>;
export type ModelTestResult = z.infer<typeof modelTestResultSchema>;
export type SessionModel = z.infer<typeof sessionModelSchema>;
export type AuthorizationRequiredEvent = z.infer<
  typeof authorizationRequiredEventSchema
>;
