import { createApi, fetchBaseQuery } from "@reduxjs/toolkit/query/react";

import { z } from "zod";

import {
  bindOwner,
  clearLiveOutput,
  controlNotice,
} from "../realtime/runtimeSlice";
import { truncateAfterUserMessage } from "./truncateAfterUserMessage";
import {
  conversationViewSchema,
  directorySelectionSchema,
  fileSelectionSchema,
  modelSettingsSchema,
  modelTestResultSchema,
  sessionModelSchema,
  sessionSummarySchema,
  workspaceSchema,
  type ConversationView,
  type DirectorySelection,
  type FileSelection,
  type ModelConfig,
  type ModelSettings,
  type ModelTestResult,
  type SessionModel,
  type SessionSummary,
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

type EditAndFork = SelectSession & {
  deliveryId: string;
  text: string;
  artifactRefs: string[];
  messageId: string;
  // 改写顶掉原身份，新分支自己成一条会话线。
  listed: boolean;
  restoreFiles: boolean;
};

type RestartFromStep = SelectSession & {
  stepId: string;
  deliveryId: string;
};

type BranchAfterTurn = SelectSession & {
  messageId: string;
};

type UploadAttachment = SelectSession & {
  file: File;
};

type AttachLocalFile = SelectSession & {
  path: string;
};

type AuthorizeCommand = SelectSession & {
  commandId: string;
  approved: boolean;
};

type ResolveControl = SelectSession & {
  requestId: string;
  approved: boolean;
};

type SetAutoAuthorize = SelectSession & {
  enabled: boolean;
};

type SetPaused = SelectSession & {
  paused: boolean;
};

type SetSessionTitle = SelectSession & {
  title: string;
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

function putConversation(
  dispatch: (action: unknown) => void,
  getState: () => unknown,
  sessionId: string,
  data: ConversationView,
) {
  const current = helpermeApi.endpoints.getConversation.select(sessionId)(
    getState() as never,
  ).data;
  if (current !== undefined && current.revision > data.revision) {
    return;
  }
  dispatch(
    helpermeApi.util.upsertQueryData("getConversation", sessionId, data),
  );
}

export const helpermeApi = createApi({
  reducerPath: "helpermeApi",
  baseQuery: fetchBaseQuery({ baseUrl: "/api" }),
  tagTypes: ["Sessions", "Workspaces", "Conversation", "SessionTitles", "ModelSettings", "SessionModel"],
  endpoints: (build) => ({
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
    getSessions: build.query<SessionSummary[], void>({
      query: () => "/sessions",
      transformResponse: (value: unknown) =>
        sessionSummarySchema.array().parse(value),
      providesTags: ["Sessions"],
    }),
    getSessionTitles: build.query<Record<string, string>, void>({
      query: () => "/session-titles",
      transformResponse: (value: unknown) =>
        z.record(z.string(), z.string()).parse(value),
      providesTags: ["SessionTitles"],
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
    selectLocalFile: build.mutation<FileSelection, void>({
      query: () => ({ url: "/files/select", method: "POST" }),
      transformResponse: (value: unknown) => fileSelectionSchema.parse(value),
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
    getConversation: build.query<ConversationView, string>({
      query: (sessionId) => `/sessions/${encodeURIComponent(sessionId)}`,
      transformResponse: (value: unknown) => conversationViewSchema.parse(value),
      providesTags: (_result, _error, sessionId) => [
        { type: "Conversation", id: sessionId },
      ],
    }),
    createSession: build.mutation<
      ConversationView,
      { connectionId: string; workspaceId: string }
    >({
      query: ({ connectionId, workspaceId }) => ({
        url: "/sessions",
        method: "POST",
        body: { connection_id: connectionId, workspace_id: workspaceId },
      }),
      transformResponse: (value: unknown) => conversationViewSchema.parse(value),
      async onQueryStarted(_arg, { dispatch, getState, queryFulfilled }) {
        const { data } = await queryFulfilled;
        dispatch(bindOwner(data.session_id));
        putConversation(dispatch, getState, data.session_id, data);
      },
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
      invalidatesTags: ["Sessions"],
      async onQueryStarted(arg, { dispatch, getState, queryFulfilled }) {
        const { data } = await queryFulfilled;
        putConversation(dispatch, getState, arg.sessionId, data);
      },
    }),
    editAndFork: build.mutation<ConversationView, EditAndFork>({
      query: ({
        connectionId,
        sessionId,
        messageId,
        deliveryId,
        text,
        listed,
        restoreFiles,
        artifactRefs,
      }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/forks`,
        method: "POST",
        body: {
          connection_id: connectionId,
          message_id: messageId,
          delivery_id: deliveryId,
          text,
          listed,
          restore_files: restoreFiles,
          artifact_refs: artifactRefs,
        },
      }),
      transformResponse: (value: unknown) => conversationViewSchema.parse(value),
      invalidatesTags: ["Sessions"],
      async onQueryStarted(arg, { dispatch, getState, queryFulfilled }) {
        const patch = dispatch(
          helpermeApi.util.updateQueryData(
            "getConversation",
            arg.sessionId,
            (draft) => {
              const truncated = truncateAfterUserMessage(
                draft,
                arg.messageId,
                arg.text.trim(),
                arg.artifactRefs,
              );
              draft.items = truncated.items;
            },
          ),
        );
        dispatch(clearLiveOutput(arg.sessionId));
        try {
          const { data } = await queryFulfilled;
          dispatch(bindOwner(data.session_id));
          putConversation(dispatch, getState, data.session_id, data);
        } catch {
          patch.undo();
        }
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
    attachLocalFile: build.mutation<AttachmentRef, AttachLocalFile>({
      query: ({ connectionId, sessionId, path }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/attachments/from-path`,
        method: "POST",
        body: { connection_id: connectionId, path },
      }),
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
    setAutoAuthorize: build.mutation<ConversationView, SetAutoAuthorize>({
      query: ({ connectionId, sessionId, enabled }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/auto-authorize`,
        method: "POST",
        body: { connection_id: connectionId, enabled },
      }),
      transformResponse: (value: unknown) => conversationViewSchema.parse(value),
      async onQueryStarted(arg, { dispatch, getState, queryFulfilled }) {
        const { data } = await queryFulfilled;
        putConversation(dispatch, getState, arg.sessionId, data);
      },
    }),
    setPaused: build.mutation<ConversationView, SetPaused>({
      query: ({ connectionId, sessionId, paused }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/paused`,
        method: "POST",
        body: { connection_id: connectionId, paused },
      }),
      transformResponse: (value: unknown) => conversationViewSchema.parse(value),
      async onQueryStarted(arg, { dispatch, getState, queryFulfilled }) {
        const { data } = await queryFulfilled;
        putConversation(dispatch, getState, arg.sessionId, data);
      },
    }),
    archiveSession: build.mutation<void, SelectSession>({
      query: ({ connectionId, sessionId }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/archive`,
        method: "POST",
        body: { connection_id: connectionId },
      }),
      invalidatesTags: ["Sessions"],
    }),
    setSessionTitle: build.mutation<{ title: string }, SetSessionTitle>({
      query: ({ connectionId, sessionId, title }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/title`,
        method: "POST",
        body: { connection_id: connectionId, title },
      }),
      transformResponse: (value: unknown) =>
        z.object({ title: z.string().min(1) }).strict().parse(value),
      invalidatesTags: ["Sessions", "SessionTitles"],
    }),
    retryTurn: build.mutation<ConversationView, SelectSession>({
      query: ({ connectionId, sessionId }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/retry`,
        method: "POST",
        body: { connection_id: connectionId },
      }),
      transformResponse: (value: unknown) => conversationViewSchema.parse(value),
      async onQueryStarted(arg, { dispatch, getState, queryFulfilled }) {
        const { data } = await queryFulfilled;
        putConversation(dispatch, getState, arg.sessionId, data);
      },
    }),
    restartFromStep: build.mutation<ConversationView, RestartFromStep>({
      query: ({ connectionId, sessionId, stepId, deliveryId }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/restarts`,
        method: "POST",
        body: {
          connection_id: connectionId,
          delivery_id: deliveryId,
          step_id: stepId,
        },
      }),
      transformResponse: (value: unknown) => conversationViewSchema.parse(value),
      invalidatesTags: ["Sessions"],
      async onQueryStarted(_arg, { dispatch, getState, queryFulfilled }) {
        const { data } = await queryFulfilled;
        dispatch(bindOwner(data.session_id));
        putConversation(dispatch, getState, data.session_id, data);
      },
    }),
    branchAfterTurn: build.mutation<ConversationView, BranchAfterTurn>({
      query: ({ connectionId, sessionId, messageId }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/branches`,
        method: "POST",
        body: {
          connection_id: connectionId,
          message_id: messageId,
        },
      }),
      transformResponse: (value: unknown) => conversationViewSchema.parse(value),
      invalidatesTags: ["Sessions"],
      async onQueryStarted(_arg, { dispatch, getState, queryFulfilled }) {
        const { data } = await queryFulfilled;
        dispatch(bindOwner(data.session_id));
        putConversation(dispatch, getState, data.session_id, data);
      },
    }),
  }),
});

export const {
  useCreateSessionMutation,
  useGetModelSettingsQuery,
  useSaveModelSettingsMutation,
  useTestModelMutation,
  useGetSessionModelQuery,
  useSetSessionModelMutation,
  useCreateWorkspaceMutation,
  useSelectWorkspaceDirectoryMutation,
  useSelectLocalFileMutation,
  useGetSessionsQuery,
  useGetSessionTitlesQuery,
  useGetWorkspacesQuery,
  useGetConversationQuery,
  useSelectSessionMutation,
  useSendInputMutation,
  useEditAndForkMutation,
  useUploadAttachmentMutation,
  useAttachLocalFileMutation,
  useCancelTurnMutation,
  useAuthorizeCommandMutation,
  useResolveControlMutation,
  useSetAutoAuthorizeMutation,
  useSetPausedMutation,
  useArchiveSessionMutation,
  useSetSessionTitleMutation,
  useRetryTurnMutation,
  useRestartFromStepMutation,
  useBranchAfterTurnMutation,
} = helpermeApi;
