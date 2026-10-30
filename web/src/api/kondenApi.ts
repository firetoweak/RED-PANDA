import { createApi, fetchBaseQuery } from "@reduxjs/toolkit/query/react";

import { z } from "zod";
import { capabilityEnabledSchema, capabilityInventorySchema, type CapabilityInventory } from "./capabilities";

import {
  bindOwner,
  controlNotice,
  type RuntimeState,
} from "../realtime/runtimeSlice";
import {
  agentRosterEntrySchema,
  externalConnectionSchema,
  createdExternalConnectionSchema,
  type ExternalConnection,
  type CreatedExternalConnection,
  externalSessionSchema,
  externalConversationSchema,
  type ExternalSessionSummary,
  type ExternalConversation,
  avatarSelectionSchema,
  avatarsSchema,
  type AvatarSelection,
  type Avatars,
  autoAuthorizeSchema,
  backgroundCommandSchema,
  type BackgroundCommand,
  conversationViewSchema,
  directorySelectionSchema,
  groupSummarySchema,
  groupViewSchema,
  modelSettingsSchema,
  modelCatalogSchema, type ModelCatalog,
  searchConfigSchema, type SearchConfig, searchResultSchema, type SearchResult,
  sharedMemorySchema,
  sharedMemoryConflictSchema,
  type SharedMemory,
  personalMemorySchema,
  personalMemoryConflictSchema,
  type PersonalMemory,
  modelTestResultSchema,
  sessionModelSchema,
  inboxItemSchema,
  routineFireSchema,
  routineSchema,
  subagentObservationSchema,
  subagentSummarySchema,
  workspaceSchema,
  type AgentRosterEntry,
  type ConversationView,
  type DirectorySelection,
  type GroupSummary,
  type GroupView,
  type ModelConfig,
  type ModelSettings,
  type ModelTestResult,
  type SessionModel,
  type InboxItem,
  type Routine,
  type RoutineFire,
  type SubagentObservation,
  type SubagentSummary,
  type Workspace,
} from "./contracts";

type SelectSession = {
  connectionId: string;
  sessionId: string;
};

type SendInput = SelectSession & {
  deliveryId: string;
  text: string;
  artifactRefs: string[];
  interrupt: boolean;
};

type UploadAttachment = SelectSession & {
  file: File;
};

type AuthorizeCommand = SelectSession & {
  commandSessionId?: string;
  commandId: string;
  approved: boolean;
};

type ResolveControl = SelectSession & {
  requestId: string;
  approved: boolean;
};

type SetAutoAuthorize = {
  connectionId: string;
  enabled: boolean;
};

const attachmentRefSchema = z.discriminatedUnion("kind", [
  z
  .object({
    kind: z.literal("image"),
    attachment_id: z.string().regex(/^sha256:[0-9a-f]{64}$/),
    name: z.string().min(1),
    size: z.number().int().nonnegative(),
    mime: z.enum(["image/png", "image/jpeg", "image/webp", "image/gif"]),
    width: z.number().int().positive(),
    height: z.number().int().positive(),
  })
  .strict(),
  z.object({
    kind: z.literal("file"),
    attachment_id: z.string().regex(/^file:[0-9a-f]{32}$/),
    name: z.string().min(1),
    size: z.number().int().nonnegative(),
  }).strict(),
]);

export type AttachmentRef = z.infer<typeof attachmentRefSchema>;

const validationIssue = z.object({ msg: z.string().min(1) }).passthrough();

export function apiErrorDetail(data: unknown): string | undefined {
  const parsed = z.object({
    detail: z.union([z.string().min(1), z.array(validationIssue).min(1)]),
  }).passthrough().safeParse(data);
  if (!parsed.success) {
    return undefined;
  }
  const detail = parsed.data.detail;
  if (typeof detail === "string") {
    return detail;
  }
  return detail.map((item) => item.msg.replace(/^Value error,\s*/, "")).join("\n");
}

function putConversation(
  dispatch: (action: unknown) => void,
  sessionId: string,
  data: ConversationView,
) {
  dispatch(
    kondenApi.util.upsertQueryData("getConversation", sessionId, data),
  );
}

export const kondenApi = createApi({
  reducerPath: "kondenApi",
  baseQuery: fetchBaseQuery({ baseUrl: "/api" }),
  tagTypes: ["Agents", "Groups", "Inbox", "Workspaces", "Conversation", "ModelSettings", "SessionModel", "Routines", "AutoAuthorize", "SharedMemory", "PersonalMemory", "Avatars", "ExternalConnections", "ExternalSessions", "Capabilities"],
  endpoints: (build) => ({
    getCapabilities: build.query<CapabilityInventory, void>({
      query: () => "/capabilities",
      transformResponse: (value: unknown) => capabilityInventorySchema.parse(value),
      providesTags: ["Capabilities"],
    }),
    setCapabilityEnabled: build.mutation<{ enabled: boolean }, {
      connectionId: string; kind: "skills" | "mcp"; id: string; enabled: boolean;
    }>({
      query: ({ connectionId, kind, id, enabled }) => ({
        url: `/capabilities/${kind}/${encodeURIComponent(id)}/enabled`, method: "PUT",
        body: { connection_id: connectionId, enabled },
      }),
      transformResponse: (value: unknown) => capabilityEnabledSchema.parse(value),
      transformErrorResponse: (response) => apiErrorDetail(response.data) ?? response,
      invalidatesTags: ["Capabilities"],
    }),
    getSharedMemory: build.query<SharedMemory, void>({
      query: () => "/shared-memory",
      transformResponse: (value: unknown) => sharedMemorySchema.parse(value),
      providesTags: ["SharedMemory"],
    }),
    saveSharedMemory: build.mutation<SharedMemory, { connectionId: string; deliveryId: string; version: number; content: string }>({
      query: ({ connectionId, deliveryId, version, content }) => ({
        url: "/shared-memory", method: "PUT", body: { connection_id: connectionId, delivery_id: deliveryId, version, content },
      }),
      transformResponse: (value: unknown) => sharedMemorySchema.parse(value),
      transformErrorResponse: (response) => response.status === 409
        && typeof response.data === "object" && response.data !== null && "current" in response.data
        ? sharedMemoryConflictSchema.parse(response.data) : response,
      invalidatesTags: ["SharedMemory"],
    }),
    getPersonalMemory: build.query<PersonalMemory, string>({
      query: (sessionId) => `/agents/${encodeURIComponent(sessionId)}/memory`,
      transformResponse: (value: unknown) => personalMemorySchema.parse(value),
      providesTags: (_result, _error, id) => [{ type: "PersonalMemory", id }],
    }),
    savePersonalMemory: build.mutation<PersonalMemory, {
      sessionId: string; connectionId: string; deliveryId: string; version: number; content: string;
    }>({
      query: ({ sessionId, connectionId, deliveryId, version, content }) => ({
        url: `/agents/${encodeURIComponent(sessionId)}/memory`, method: "PUT",
        body: { connection_id: connectionId, delivery_id: deliveryId, version, content },
      }),
      transformResponse: (value: unknown) => personalMemorySchema.parse(value),
      transformErrorResponse: (response) => response.status === 409
        && typeof response.data === "object" && response.data !== null && "current" in response.data
        ? personalMemoryConflictSchema.parse(response.data) : response,
      invalidatesTags: (_result, _error, { sessionId }) => [{ type: "PersonalMemory", id: sessionId }],
    }),
    getModelSettings: build.query<ModelSettings, void>({
      query: () => "/model-settings",
      transformResponse: (value: unknown) => modelSettingsSchema.parse(value),
      providesTags: ["ModelSettings"],
    }),
    getModelCatalog: build.query<ModelCatalog, string>({
      query: (provider) => `/model-settings/catalog/${encodeURIComponent(provider)}`,
      transformResponse: (value: unknown) => modelCatalogSchema.parse(value),
      transformErrorResponse: (response) => response.status === 400 || response.status === 502
        ? z.object({ detail: z.string() }).strict().parse(response.data).detail : response,
    }),
    getSearchConfig: build.query<SearchConfig, void>({
      query: () => "/model-settings/search",
      transformResponse: (value: unknown) => searchConfigSchema.parse(value),
    }),
    saveSearchConfig: build.mutation<SearchConfig, { connectionId: string; config: SearchConfig }>({
      query: ({ connectionId, config }) => ({url: "/model-settings/search", method: "PUT", body: {connection_id: connectionId, config}}),
      transformResponse: (value: unknown) => searchConfigSchema.parse(value),
      transformErrorResponse: (response) => response.status === 400
        ? z.object({ detail: z.string() }).strict().parse(response.data).detail : response,
    }),
    testSearch: build.mutation<SearchResult, { connectionId: string; config: SearchConfig; query: string }>({
      query: ({ connectionId, config, query }) => ({url: "/model-settings/search/test", method: "POST", body: {connection_id: connectionId, config, query}}),
      transformResponse: (value: unknown) => searchResultSchema.parse(value),
      transformErrorResponse: (response) => response.status === 400 || response.status === 502
        ? z.object({ detail: z.string() }).strict().parse(response.data).detail : response,
    }),
    saveModelSettings: build.mutation<ModelSettings, { connectionId: string; config: ModelConfig }>({
      query: ({ connectionId, config }) => ({
        url: "/model-settings", method: "PUT", body: { connection_id: connectionId, config },
      }),
      transformResponse: (value: unknown) => modelSettingsSchema.parse(value),
      transformErrorResponse: (response) => response.status === 400 || response.status === 409
        ? z.object({ detail: z.string() }).strict().parse(response.data).detail : response,
      invalidatesTags: ["ModelSettings", "SessionModel"],
    }),
    testModel: build.mutation<ModelTestResult, { connectionId: string; model: string; reasoningEffort?: string | null }>({
      query: ({ connectionId, model, reasoningEffort }) => ({
        url: "/model-settings/test", method: "POST", body: { connection_id: connectionId, model, reasoning_effort: reasoningEffort ?? null },
      }),
      transformResponse: (value: unknown) => modelTestResultSchema.parse(value),
      transformErrorResponse: (response) => response.status === 400
        ? z.object({ detail: z.string() }).strict().parse(response.data).detail : response,
    }),
    getSessionModel: build.query<SessionModel, string>({
      query: (sessionId) => `/sessions/${encodeURIComponent(sessionId)}/model`,
      transformResponse: (value: unknown) => sessionModelSchema.parse(value),
      providesTags: (_result, _error, id) => [{ type: "SessionModel", id }],
    }),
    setSessionModel: build.mutation<SessionModel, SelectSession & { model: string; reasoningEffort?: string | null }>({
      query: ({ connectionId, sessionId, model, reasoningEffort }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/model`, method: "PUT",
        body: { connection_id: connectionId, model, reasoning_effort: reasoningEffort ?? null },
      }),
      transformResponse: (value: unknown) => sessionModelSchema.parse(value),
      transformErrorResponse: (response) => response.status === 400
        ? z.object({ detail: z.string() }).strict().parse(response.data).detail : response,
      invalidatesTags: (_result, _error, { sessionId }) => [{ type: "SessionModel", id: sessionId }],
    }),
    getAvatars: build.query<Avatars, void>({
      query: () => "/avatars",
      transformResponse: (value: unknown) => avatarsSchema.parse(value),
      providesTags: ["Avatars"],
    }),
    setAgentAvatar: build.mutation<AvatarSelection | null, SelectSession & { seed: string | null }>({
      query: ({ connectionId, sessionId, seed }) => ({
        url: `/agents/${encodeURIComponent(sessionId)}/avatar`, method: "PUT",
        body: { connection_id: connectionId, seed },
      }),
      transformResponse: (value: unknown) => avatarSelectionSchema.nullable().parse(value),
      transformErrorResponse: (response) => apiErrorDetail(response.data) ?? response,
      invalidatesTags: (_result, error) => error === undefined ? ["Avatars"] : [],
    }),
    uploadAgentAvatar: build.mutation<AvatarSelection, UploadAttachment>({
      query: ({ connectionId, sessionId, file }) => {
        const body = new FormData();
        body.append("connection_id", connectionId);
        body.append("file", file);
        return { url: `/agents/${encodeURIComponent(sessionId)}/avatar/image`, method: "POST", body };
      },
      transformResponse: (value: unknown) => avatarSelectionSchema.parse(value),
      transformErrorResponse: (response) => apiErrorDetail(response.data) ?? response,
      invalidatesTags: (_result, error) => error === undefined ? ["Avatars"] : [],
    }),
    getAgents: build.query<AgentRosterEntry[], void>({
      query: () => "/agents",
      transformResponse: (value: unknown) =>
        agentRosterEntrySchema.array().parse(value),
      providesTags: ["Agents"],
    }),
    getExternalConnections: build.query<ExternalConnection[], void>({
      query: () => "/external-connections",
      transformResponse: (value: unknown) => externalConnectionSchema.array().parse(value),
      providesTags: ["ExternalConnections"],
    }),
    getExternalSessions: build.query<ExternalSessionSummary[], void>({
      query: () => "/external-sessions",
      transformResponse: (value: unknown) => z.array(externalSessionSchema).parse(value),
      providesTags: ["ExternalSessions"],
    }),
    getExternalConversation: build.query<ExternalConversation, string>({
      query: (identity) => `/external-sessions/${encodeURIComponent(identity)}`,
      transformResponse: (value: unknown) => externalConversationSchema.parse(value),
      providesTags: ["ExternalSessions"],
    }),
    deleteExternalSession: build.mutation<void, { connectionId: string; identity: string }>({
      query: ({ connectionId, identity }) => ({
        url: `/external-sessions/${encodeURIComponent(identity)}`,
        method: "DELETE", body: { connection_id: connectionId },
      }),
      transformErrorResponse: (response) => apiErrorDetail(response.data) ?? response,
      invalidatesTags: (_result, error) => error === undefined ? ["ExternalSessions"] : [],
    }),
    setExternalCodex: build.mutation<ExternalConversation, {
      connectionId: string; identity: string; target: { executable: string } | null;
    }>({
      query: ({ connectionId, identity, target }) => ({
        url: `/external-sessions/${encodeURIComponent(identity)}/codex`,
        method: target === null ? "DELETE" : "PUT",
        body: { connection_id: connectionId, ...target },
      }),
      transformResponse: (value: unknown) => externalConversationSchema.parse(value),
      transformErrorResponse: (response) => apiErrorDetail(response.data) ?? response,
      invalidatesTags: (_result, error) => error === undefined ? ["ExternalSessions"] : [],
    }),
    retryExternalCodex: build.mutation<ExternalConversation, { connectionId: string; identity: string }>({
      query: ({ connectionId, identity }) => ({
        url: `/external-sessions/${encodeURIComponent(identity)}/codex/retry`,
        method: "POST", body: { connection_id: connectionId },
      }),
      transformResponse: (value: unknown) => externalConversationSchema.parse(value),
      transformErrorResponse: (response) => apiErrorDetail(response.data) ?? response,
      invalidatesTags: (_result, error) => error === undefined ? ["ExternalSessions"] : [],
    }),
    createExternalConnection: build.mutation<CreatedExternalConnection, { connectionId: string; name: string }>({
      query: ({ connectionId, name }) => ({
        url: "/external-connections", method: "POST", body: { connection_id: connectionId, name },
      }),
      transformResponse: (value: unknown) => createdExternalConnectionSchema.parse(value),
      transformErrorResponse: (response) => apiErrorDetail(response.data) ?? response,
      invalidatesTags: (_result, error) => error === undefined ? ["ExternalConnections"] : [],
    }),
    viewExternalConnection: build.mutation<CreatedExternalConnection, { connectionId: string; externalId: string }>({
      query: ({ connectionId, externalId }) => ({
        url: `/external-connections/${externalId}/configuration`, method: "POST", body: { connection_id: connectionId },
      }),
      transformResponse: (value: unknown) => createdExternalConnectionSchema.parse(value),
      transformErrorResponse: (response) => apiErrorDetail(response.data) ?? response,
    }),
    renameExternalConnection: build.mutation<ExternalConnection, { connectionId: string; externalId: string; name: string }>({
      query: ({ connectionId, externalId, name }) => ({
        url: `/external-connections/${externalId}/name`, method: "PUT", body: { connection_id: connectionId, name },
      }),
      transformResponse: (value: unknown) => externalConnectionSchema.parse(value),
      transformErrorResponse: (response) => apiErrorDetail(response.data) ?? response,
      invalidatesTags: (_result, error) => error === undefined ? ["ExternalConnections"] : [],
    }),
    revokeExternalConnection: build.mutation<void, { connectionId: string; externalId: string }>({
      query: ({ connectionId, externalId }) => ({
        url: `/external-connections/${encodeURIComponent(externalId)}`,
        method: "DELETE", body: { connection_id: connectionId },
      }),
      transformErrorResponse: (response) => apiErrorDetail(response.data) ?? response,
      invalidatesTags: (_result, error) => error === undefined ? ["ExternalConnections"] : [],
    }),
    deleteAgent: build.mutation<void, SelectSession>({
      query: ({ connectionId, sessionId }) => ({
        url: `/agents/${encodeURIComponent(sessionId)}`, method: "DELETE", body: { connection_id: connectionId },
      }),
      transformErrorResponse: (response) => apiErrorDetail(response.data) ?? response,
      invalidatesTags: ["Agents", "Groups", "Inbox", "Conversation", "Routines"],
    }),
    dissolveGroup: build.mutation<void, { connectionId: string; groupId: string }>({
      query: ({ connectionId, groupId }) => ({
        url: `/groups/${encodeURIComponent(groupId)}`, method: "DELETE", body: { connection_id: connectionId },
      }),
      transformErrorResponse: (response) => apiErrorDetail(response.data) ?? response,
      invalidatesTags: ["Groups"],
    }),
    getGroups: build.query<GroupSummary[], void>({
      query: () => "/groups",
      transformResponse: (value: unknown) => groupSummarySchema.array().parse(value),
      providesTags: ["Groups"],
    }),
    getGroup: build.query<GroupView, string>({
      query: (groupId) => `/groups/${encodeURIComponent(groupId)}`,
      transformResponse: (value: unknown) => groupViewSchema.parse(value),
      providesTags: ["Groups"],
    }),
    createGroup: build.mutation<
      GroupView,
      { connectionId: string; name: string; members: string[] }
    >({
      query: ({ connectionId, name, members }) => ({
        url: "/groups",
        method: "POST",
        body: { connection_id: connectionId, name, members },
      }),
      transformResponse: (value: unknown) => groupViewSchema.parse(value),
      transformErrorResponse: (response) => {
        if (response.status !== 400 && response.status !== 409 && response.status !== 422 && response.status !== 404) {
          return response;
        }
        return apiErrorDetail(response.data) ?? response;
      },
      invalidatesTags: ["Groups"],
    }),
    postGroupMessage: build.mutation<
      GroupView,
      { connectionId: string; groupId: string; text: string; mention: string[]; deliveryId: string }
    >({
      query: ({ connectionId, groupId, text, mention, deliveryId }) => ({
        url: `/groups/${encodeURIComponent(groupId)}/messages`,
        method: "POST",
        body: { connection_id: connectionId, text, mention, delivery_id: deliveryId },
      }),
      transformResponse: (value: unknown) => groupViewSchema.parse(value),
      transformErrorResponse: (response) => response.status === 422 || response.status === 404
        ? apiErrorDetail(response.data) ?? response : response,
      invalidatesTags: ["Groups"],
    }),
    getBackgroundCommands: build.query<BackgroundCommand[], string>({
      query: (sessionId) => `/sessions/${encodeURIComponent(sessionId)}/commands`,
      transformResponse: (value: unknown) => backgroundCommandSchema.array().parse(value),
    }),
    stopBackgroundCommand: build.mutation<BackgroundCommand, SelectSession & { commandId: string }>({
      query: ({ sessionId, connectionId, commandId }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/commands/${encodeURIComponent(commandId)}/stop`,
        method: "POST", body: { connection_id: connectionId },
      }),
      transformResponse: (value: unknown) => backgroundCommandSchema.parse(value),
    }),
    getRoutines: build.query<Routine[], string>({
      query: (sessionId) => `/sessions/${encodeURIComponent(sessionId)}/routines`,
      transformResponse: (value: unknown) => routineSchema.array().parse(value),
      providesTags: (_result, _error, id) => [{ type: "Routines", id }],
    }),
    getRoutineFires: build.query<RoutineFire[], { sessionId: string; routineId: string }>({
      query: ({ sessionId, routineId }) =>
        `/sessions/${encodeURIComponent(sessionId)}/routines/${encodeURIComponent(routineId)}/fires`,
      transformResponse: (value: unknown) => routineFireSchema.array().parse(value),
      providesTags: (_result, _error, { sessionId }) => [{ type: "Routines", id: sessionId }],
    }),
    setRoutineState: build.mutation<
      Routine[],
      SelectSession & { routineId: string; state: "active" | "paused_by_user" | "deleted" }
    >({
      query: ({ connectionId, sessionId, routineId, state }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/routines/${encodeURIComponent(routineId)}/state`,
        method: "PUT",
        body: { connection_id: connectionId, state },
      }),
      transformResponse: (value: unknown) => routineSchema.array().parse(value),
      transformErrorResponse: (response) => response.status === 404 || response.status === 409
        ? apiErrorDetail(response.data) ?? response : response,
      invalidatesTags: (_result, _error, { sessionId }) => [{ type: "Routines", id: sessionId }],
    }),
    getInbox: build.query<InboxItem[], void>({
      query: () => "/inbox",
      transformResponse: (value: unknown) => inboxItemSchema.array().parse(value),
      providesTags: ["Inbox"],
    }),
    answerAbsence: build.mutation<unknown, { connectionId: string; sessionId: string; action: "continue" | "stop" }>({
      query: ({ connectionId, sessionId, action }) => ({
        url: "/inbox/absence",
        method: "POST",
        body: { connection_id: connectionId, session_id: sessionId, action },
      }),
      invalidatesTags: ["Inbox", "Routines"],
    }),
    createAgent: build.mutation<
      AgentRosterEntry,
      { connectionId: string; path: string | null; name: string }
    >({
      query: ({ connectionId, path, name }) => ({
        url: "/agents",
        method: "POST",
        body: { connection_id: connectionId, path, name },
      }),
      transformResponse: (value: unknown) => agentRosterEntrySchema.parse(value),
      transformErrorResponse: (response) => {
        if (response.status !== 400 && response.status !== 409 && response.status !== 422 && response.status !== 404) {
          return response;
        }
        return apiErrorDetail(response.data) ?? response;
      },
      invalidatesTags: ["Agents", "Workspaces"],
      async onQueryStarted(_arg, { dispatch, queryFulfilled }) {
        const { data } = await queryFulfilled;
        dispatch(bindOwner(data.session_id));
      },
    }),
    renameAgent: build.mutation<AgentRosterEntry, { connectionId: string; sessionId: string; name: string }>({
      query: ({ connectionId, sessionId, name }) => ({
        url: `/agents/${encodeURIComponent(sessionId)}/name`,
        method: "PUT",
        body: { connection_id: connectionId, name },
      }),
      transformResponse: (value: unknown) => agentRosterEntrySchema.parse(value),
      transformErrorResponse: (response) => apiErrorDetail(response.data) ?? response,
      invalidatesTags: ["Agents", "Groups", "Inbox", "Conversation"],
    }),
    linkProject: build.mutation<
      AgentRosterEntry,
      { connectionId: string; sessionId: string; workspaceId: string | null; path: string | null }
    >({
      query: ({ connectionId, sessionId, workspaceId, path }) => ({
        url: `/agents/${encodeURIComponent(sessionId)}/projects`,
        method: "POST",
        body: { connection_id: connectionId, workspace_id: workspaceId, path },
      }),
      transformResponse: (value: unknown) => agentRosterEntrySchema.parse(value),
      transformErrorResponse: (response) => apiErrorDetail(response.data) ?? response,
      invalidatesTags: ["Agents", "Workspaces"],
    }),
    unlinkProject: build.mutation<AgentRosterEntry, { connectionId: string; sessionId: string; name: string }>({
      query: ({ connectionId, sessionId, name }) => ({
        url: `/agents/${encodeURIComponent(sessionId)}/projects/${encodeURIComponent(name)}/unlink`,
        method: "POST",
        body: { connection_id: connectionId },
      }),
      transformResponse: (value: unknown) => agentRosterEntrySchema.parse(value),
      transformErrorResponse: (response) => apiErrorDetail(response.data) ?? response,
      invalidatesTags: ["Agents"],
    }),
    getWorkspaces: build.query<Workspace[], void>({
      query: () => "/workspaces",
      transformResponse: (value: unknown) =>
        workspaceSchema.array().parse(value),
      providesTags: ["Workspaces"],
    }),
    selectWorkspaceDirectory: build.mutation<DirectorySelection, void>({
      query: () => ({ url: "/workspaces/select-directory", method: "POST" }),
      transformResponse: (value: unknown) => directorySelectionSchema.parse(value),
      transformErrorResponse: (response) =>
        response.status === 503
          ? z.object({ detail: z.string().min(1) }).strict().parse(response.data).detail
          : response,
    }),
    selectLocalFile: build.mutation<{ file: AttachmentRef | null }, SelectSession>({
      query: ({ connectionId, sessionId }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/attachments/select`,
        method: "POST",
        body: { connection_id: connectionId },
      }),
      transformResponse: (value: unknown) => z.object({ file: attachmentRefSchema.nullable() }).strict().parse(value),
      transformErrorResponse: (response) =>
        response.status === 503
          ? z.object({ detail: z.string().min(1) }).strict().parse(response.data).detail
          : response,
    }),
    getConversation: build.query<ConversationView, string>({
      query: (sessionId) => `/sessions/${encodeURIComponent(sessionId)}`,
      transformResponse: (value: unknown) => conversationViewSchema.parse(value),
      merge(current, incoming) {
        if (incoming.revision >= current.revision) return incoming;
      },
      providesTags: (_result, _error, sessionId) => [
        { type: "Conversation", id: sessionId },
      ],
    }),
    getSubagents: build.query<SubagentSummary[], string>({
      query: (sessionId) => `/sessions/${encodeURIComponent(sessionId)}/subagents`,
      transformResponse: (value: unknown) => subagentSummarySchema.array().parse(value),
      providesTags: (result, _error, sessionId) => [
        { type: "Conversation", id: sessionId },
        ...(result ?? []).map((child) => ({ type: "Conversation" as const, id: child.child_session_id })),
      ],
    }),
    observeSubagent: build.query<SubagentObservation, { parentSessionId: string; commandId: string }>({
      query: ({ parentSessionId, commandId }) =>
        `/sessions/${encodeURIComponent(parentSessionId)}/subagents/${encodeURIComponent(commandId)}`,
      transformResponse: (value: unknown) => subagentObservationSchema.parse(value),
      providesTags: (result, _error, { parentSessionId }) => [
        { type: "Conversation", id: parentSessionId },
        ...(result === undefined ? [] : [{ type: "Conversation" as const, id: result.session_id }]),
      ],
    }),
    selectSession: build.mutation<ConversationView, SelectSession>({
      query: ({ connectionId, sessionId }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/select`,
        method: "POST",
        body: { connection_id: connectionId },
      }),
      transformResponse: (value: unknown) => conversationViewSchema.parse(value),
      async onQueryStarted(arg, { dispatch, getState, queryFulfilled }) {
        const { data } = await queryFulfilled;
        const { runtime } = getState() as unknown as { runtime: RuntimeState };
        if (runtime.connectionId === arg.connectionId && runtime.viewingSessionId === arg.sessionId) {
          dispatch(bindOwner(arg.sessionId));
        }
        putConversation(dispatch, arg.sessionId, data);
      },
    }),
    sendInput: build.mutation<ConversationView, SendInput>({
      query: ({ connectionId, sessionId, deliveryId, text, artifactRefs, interrupt }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/inputs`,
        method: "POST",
        body: {
          connection_id: connectionId,
          delivery_id: deliveryId,
          text,
          artifact_refs: artifactRefs,
          interrupt,
        },
      }),
      transformResponse: (value: unknown) => conversationViewSchema.parse(value),
      async onQueryStarted(arg, { dispatch, queryFulfilled }) {
        const { data } = await queryFulfilled;
        putConversation(dispatch, arg.sessionId, data);
      },
    }),
    uploadAttachment: build.mutation<AttachmentRef, UploadAttachment>({
      query: ({ connectionId, sessionId, file }) => {
        const body = new FormData();
        body.append("connection_id", connectionId);
        body.append("file", file);
        return {
          url: `/sessions/${encodeURIComponent(sessionId)}/attachments`,
          method: "POST",
          body,
        };
      },
      transformResponse: (value: unknown) => attachmentRefSchema.parse(value),
    }),
    cancelTurn: build.mutation<ConversationView, SelectSession>({
      query: ({ connectionId, sessionId }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/cancel`,
        method: "POST",
        body: { connection_id: connectionId },
      }),
      transformResponse: (value: unknown) => conversationViewSchema.parse(value),
      async onQueryStarted(arg, { dispatch, queryFulfilled }) {
        const { data } = await queryFulfilled;
        putConversation(dispatch, arg.sessionId, data);
      },
    }),
    authorizeCommand: build.mutation<ConversationView, AuthorizeCommand>({
      query: ({ connectionId, sessionId, commandId, approved, commandSessionId }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/commands/${encodeURIComponent(commandId)}/authorize`,
        method: "POST",
        body: { connection_id: connectionId, approved, ...(commandSessionId === undefined ? {} : { command_session_id: commandSessionId }) },
      }),
      transformResponse: (value: unknown) => conversationViewSchema.parse(value),
      invalidatesTags: ["Agents", "Inbox"],
      async onQueryStarted(arg, { dispatch, queryFulfilled }) {
        const { data } = await queryFulfilled;
        putConversation(dispatch, arg.sessionId, data);
      },
    }),
    resolveControl: build.mutation<ConversationView, ResolveControl>({
      query: ({ connectionId, sessionId, requestId, approved }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/control`,
        method: "POST",
        body: { connection_id: connectionId, request_id: requestId, approved },
      }),
      transformResponse: (value: unknown) => conversationViewSchema.parse(value),
      invalidatesTags: ["Agents", "Inbox"],
      async onQueryStarted(arg, { dispatch, queryFulfilled }) {
        const { data } = await queryFulfilled;
        putConversation(dispatch, arg.sessionId, data);
        dispatch(
          controlNotice({
            sessionId: arg.sessionId,
            message: data.session.control_message,
          }),
        );
      },
    }),
    getAutoAuthorize: build.query<{ enabled: boolean }, void>({
      query: () => "/auto-authorize",
      transformResponse: (value: unknown) => autoAuthorizeSchema.parse(value),
      providesTags: ["AutoAuthorize"],
    }),
    setAutoAuthorize: build.mutation<{ enabled: boolean }, SetAutoAuthorize>({
      query: ({ connectionId, enabled }) => ({
        url: "/auto-authorize",
        method: "POST",
        body: { connection_id: connectionId, enabled },
      }),
      transformResponse: (value: unknown) => autoAuthorizeSchema.parse(value),
      invalidatesTags: ["AutoAuthorize", "Conversation", "Inbox", "Agents"],
    }),
    retryTurn: build.mutation<ConversationView, SelectSession>({
      query: ({ connectionId, sessionId }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/retry`,
        method: "POST",
        body: { connection_id: connectionId },
      }),
      transformResponse: (value: unknown) => conversationViewSchema.parse(value),
      invalidatesTags: ["Inbox"],
      async onQueryStarted(arg, { dispatch, queryFulfilled }) {
        const { data } = await queryFulfilled;
        putConversation(dispatch, arg.sessionId, data);
      },
    }),
  }),
});

export const {
  useGetCapabilitiesQuery,
  useSetCapabilityEnabledMutation,
  useGetExternalConnectionsQuery,
  useGetExternalSessionsQuery,
  useGetExternalConversationQuery,
  useDeleteExternalSessionMutation,
  useSetExternalCodexMutation,
  useRetryExternalCodexMutation,
  useCreateExternalConnectionMutation,
  useViewExternalConnectionMutation,
  useRevokeExternalConnectionMutation,
  useRenameExternalConnectionMutation,
  useGetBackgroundCommandsQuery,
  useStopBackgroundCommandMutation,
  useGetRoutinesQuery,
  useGetRoutineFiresQuery,
  useSetRoutineStateMutation,
  useLinkProjectMutation,
  useUnlinkProjectMutation,
  useGetModelSettingsQuery,
  useGetSharedMemoryQuery,
  useSaveSharedMemoryMutation,
  useGetPersonalMemoryQuery,
  useSavePersonalMemoryMutation,
  useSaveModelSettingsMutation,
  useGetModelCatalogQuery,
  useTestModelMutation,
  useGetSessionModelQuery,
  useSetSessionModelMutation,
  useSelectWorkspaceDirectoryMutation,
  useSelectLocalFileMutation,
  useGetAgentsQuery,
  useGetAvatarsQuery,
  useSetAgentAvatarMutation,
  useUploadAgentAvatarMutation,
  useDeleteAgentMutation,
  useDissolveGroupMutation,
  useCreateAgentMutation,
  useRenameAgentMutation,
  useGetInboxQuery,
  useAnswerAbsenceMutation,
  useGetGroupsQuery,
  useGetGroupQuery,
  useCreateGroupMutation,
  usePostGroupMessageMutation,
  useGetWorkspacesQuery,
  useGetConversationQuery,
  useGetSubagentsQuery,
  useObserveSubagentQuery,
  useSelectSessionMutation,
  useSendInputMutation,
  useUploadAttachmentMutation,
  useCancelTurnMutation,
  useAuthorizeCommandMutation,
  useResolveControlMutation,
  useSetAutoAuthorizeMutation,
  useGetAutoAuthorizeQuery,
  useRetryTurnMutation,
} = kondenApi;
