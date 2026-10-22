import { createApi, fetchBaseQuery } from "@reduxjs/toolkit/query/react";

import { z } from "zod";
import { fileChangesSchema, fileDiffSchema, type FileChanges, type FileDiff } from "./fileChanges";

import {
  bindOwner,
  controlNotice,
} from "../realtime/runtimeSlice";
import {
  agentRosterEntrySchema,
  autoAuthorizeSchema,
  backgroundCommandSchema,
  type BackgroundCommand,
  conversationViewSchema,
  directorySelectionSchema,
  groupSummarySchema,
  groupViewSchema,
  modelSettingsSchema,
  modelTestResultSchema,
  sessionModelSchema,
  inboxItemSchema,
  routineFireSchema,
  routineSchema,
  subagentObservationSchema,
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
};

type UploadAttachment = SelectSession & {
  file: File;
};

type AuthorizeCommand = SelectSession & {
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
  getState: () => unknown,
  sessionId: string,
  data: ConversationView,
) {
  const current = redpandaApi.endpoints.getConversation.select(sessionId)(
    getState() as never,
  ).data;
  if (current !== undefined && current.revision > data.revision) {
    return;
  }
  dispatch(
    redpandaApi.util.upsertQueryData("getConversation", sessionId, data),
  );
}

export const redpandaApi = createApi({
  reducerPath: "redpandaApi",
  baseQuery: fetchBaseQuery({ baseUrl: "/api" }),
  tagTypes: ["Agents", "Groups", "Inbox", "Workspaces", "Conversation", "ModelSettings", "SessionModel", "FileChanges", "Routines", "AutoAuthorize"],
  endpoints: (build) => ({
    getFileChanges: build.query<FileChanges, { sessionId: string; project?: string }>({
      query: ({ sessionId, project }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/changes`, params: project === undefined ? {} : { project },
      }),
      transformResponse: (value: unknown) => fileChangesSchema.parse(value),
      providesTags: ["FileChanges"],
    }),
    getFileDiff: build.query<FileDiff, { sessionId: string; project?: string; path: string; revision: string; offset: number }>({
      query: ({ sessionId, ...params }) => ({ url: `/sessions/${encodeURIComponent(sessionId)}/changes`, params }),
      transformResponse: (value: unknown) => fileDiffSchema.parse(value),
      providesTags: ["FileChanges"],
    }),
    getModelSettings: build.query<ModelSettings, void>({
      query: () => "/model-settings",
      transformResponse: (value: unknown) => modelSettingsSchema.parse(value),
      providesTags: ["ModelSettings"],
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
    testModel: build.mutation<ModelTestResult, { connectionId: string; model: string }>({
      query: ({ connectionId, model }) => ({
        url: "/model-settings/test", method: "POST", body: { connection_id: connectionId, model },
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
    setSessionModel: build.mutation<SessionModel, SelectSession & { model: string }>({
      query: ({ connectionId, sessionId, model }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/model`, method: "PUT",
        body: { connection_id: connectionId, model },
      }),
      transformResponse: (value: unknown) => sessionModelSchema.parse(value),
      transformErrorResponse: (response) => response.status === 400
        ? z.object({ detail: z.string() }).strict().parse(response.data).detail : response,
      invalidatesTags: (_result, _error, { sessionId }) => [{ type: "SessionModel", id: sessionId }],
    }),
    getAgents: build.query<AgentRosterEntry[], void>({
      query: () => "/agents",
      transformResponse: (value: unknown) =>
        agentRosterEntrySchema.array().parse(value),
      providesTags: ["Agents"],
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
      { connectionId: string; groupId: string; text: string; mention: string[] }
    >({
      query: ({ connectionId, groupId, text, mention }) => ({
        url: `/groups/${encodeURIComponent(groupId)}/messages`,
        method: "POST",
        body: { connection_id: connectionId, text, mention },
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
      { connectionId: string; workspaceId: string | null; name: string }
    >({
      query: ({ connectionId, workspaceId, name }) => ({
        url: "/agents",
        method: "POST",
        body: { connection_id: connectionId, workspace_id: workspaceId, name },
      }),
      transformResponse: (value: unknown) => agentRosterEntrySchema.parse(value),
      transformErrorResponse: (response) => {
        if (response.status !== 400 && response.status !== 409 && response.status !== 422 && response.status !== 404) {
          return response;
        }
        return apiErrorDetail(response.data) ?? response;
      },
      invalidatesTags: ["Agents"],
      async onQueryStarted(_arg, { dispatch, queryFulfilled }) {
        const { data } = await queryFulfilled;
        dispatch(bindOwner(data.session_id));
      },
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
      invalidatesTags: ["Agents", "Workspaces", "FileChanges"],
    }),
    unlinkProject: build.mutation<AgentRosterEntry, { connectionId: string; sessionId: string; name: string }>({
      query: ({ connectionId, sessionId, name }) => ({
        url: `/agents/${encodeURIComponent(sessionId)}/projects/${encodeURIComponent(name)}/unlink`,
        method: "POST",
        body: { connection_id: connectionId },
      }),
      transformResponse: (value: unknown) => agentRosterEntrySchema.parse(value),
      transformErrorResponse: (response) => apiErrorDetail(response.data) ?? response,
      invalidatesTags: ["Agents", "FileChanges"],
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
    createWorkspace: build.mutation<
      Workspace,
      { name: string; taskRoot: string; fullAccess: boolean }
    >({
      query: ({ name, taskRoot, fullAccess }) => ({
        url: "/workspaces",
        method: "POST",
        body: { name, task_root: taskRoot, full_access: fullAccess },
      }),
      transformResponse: (value: unknown) => workspaceSchema.parse(value),
      invalidatesTags: ["Workspaces"],
    }),
    renameWorkspace: build.mutation<Workspace, { workspaceId: string; name: string }>({
      query: ({ workspaceId, name }) => ({
        url: `/workspaces/${encodeURIComponent(workspaceId)}`,
        method: "PATCH",
        body: { name },
      }),
      transformResponse: (value: unknown) => workspaceSchema.parse(value),
      invalidatesTags: ["Workspaces"],
    }),
    deleteWorkspace: build.mutation<void, { workspaceId: string }>({
      query: ({ workspaceId }) => ({
        url: `/workspaces/${encodeURIComponent(workspaceId)}`,
        method: "DELETE",
      }),
      invalidatesTags: ["Workspaces", "Agents", "Inbox"],
    }),
    getConversation: build.query<ConversationView, string>({
      query: (sessionId) => `/sessions/${encodeURIComponent(sessionId)}`,
      transformResponse: (value: unknown) => conversationViewSchema.parse(value),
      providesTags: (_result, _error, sessionId) => [
        { type: "Conversation", id: sessionId },
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
        dispatch(bindOwner(arg.sessionId));
        putConversation(dispatch, getState, arg.sessionId, data);
      },
    }),
    sendInput: build.mutation<ConversationView, SendInput>({
      query: ({ connectionId, sessionId, deliveryId, text, artifactRefs }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/inputs`,
        method: "POST",
        body: {
          connection_id: connectionId,
          delivery_id: deliveryId,
          text,
          artifact_refs: artifactRefs,
        },
      }),
      transformResponse: (value: unknown) => conversationViewSchema.parse(value),
      async onQueryStarted(arg, { dispatch, getState, queryFulfilled }) {
        const { data } = await queryFulfilled;
        putConversation(dispatch, getState, arg.sessionId, data);
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
      async onQueryStarted(arg, { dispatch, getState, queryFulfilled }) {
        const { data } = await queryFulfilled;
        putConversation(dispatch, getState, arg.sessionId, data);
      },
    }),
    authorizeCommand: build.mutation<ConversationView, AuthorizeCommand>({
      query: ({ connectionId, sessionId, commandId, approved }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/commands/${encodeURIComponent(commandId)}/authorize`,
        method: "POST",
        body: { connection_id: connectionId, approved },
      }),
      transformResponse: (value: unknown) => conversationViewSchema.parse(value),
      invalidatesTags: ["Agents", "Inbox"],
      async onQueryStarted(arg, { dispatch, getState, queryFulfilled }) {
        const { data } = await queryFulfilled;
        putConversation(dispatch, getState, arg.sessionId, data);
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
      async onQueryStarted(arg, { dispatch, getState, queryFulfilled }) {
        const { data } = await queryFulfilled;
        putConversation(dispatch, getState, arg.sessionId, data);
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
      async onQueryStarted(arg, { dispatch, getState, queryFulfilled }) {
        const { data } = await queryFulfilled;
        putConversation(dispatch, getState, arg.sessionId, data);
      },
    }),
  }),
});

export const {
  useGetBackgroundCommandsQuery,
  useStopBackgroundCommandMutation,
  useGetRoutinesQuery,
  useGetRoutineFiresQuery,
  useSetRoutineStateMutation,
  useGetFileChangesQuery,
  useLinkProjectMutation,
  useUnlinkProjectMutation,
  useGetFileDiffQuery,
  useGetModelSettingsQuery,
  useSaveModelSettingsMutation,
  useTestModelMutation,
  useGetSessionModelQuery,
  useSetSessionModelMutation,
  useCreateWorkspaceMutation,
  useRenameWorkspaceMutation,
  useDeleteWorkspaceMutation,
  useSelectWorkspaceDirectoryMutation,
  useSelectLocalFileMutation,
  useGetAgentsQuery,
  useCreateAgentMutation,
  useGetInboxQuery,
  useAnswerAbsenceMutation,
  useGetGroupsQuery,
  useGetGroupQuery,
  useCreateGroupMutation,
  usePostGroupMessageMutation,
  useGetWorkspacesQuery,
  useGetConversationQuery,
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
} = redpandaApi;
