import {
  ActionIcon,
  Alert,
  Box,
  Button,
  Center,
  Group,
  Loader,
  Stack,
  Text,
  ThemeIcon,
} from "@mantine/core";
import {
  IconAlertCircle,
  IconArrowDown,
  IconCheck,
  IconCopy,
  IconSparkles,
} from "@tabler/icons-react";
import { useEffect, useRef, useState } from "react";
import type { VirtuosoHandle } from "react-virtuoso";
import { useParams } from "react-router-dom";

import {
  useAuthorizeCommandMutation,
  useCancelTurnMutation,
  useGetConversationQuery,
  useResolveControlMutation,
  useSelectSessionMutation,
  useSendInputMutation,
  useSetAutoAuthorizeMutation,
  useRetryTurnMutation,
} from "../../api/redpandaApi";
import { useAppDispatch, useAppSelector } from "../../app/hooks";
import { redPandaMark } from "../../app/brand";
import { ConversationWelcome } from "../../app/ConversationWelcome";
import {
  controlNotice,
  viewing,
} from "../../realtime/runtimeSlice";
import { Composer } from "./Composer";
import { FileChangesPanel, FileChangesSummary } from "./FileChangesPanel";
import { createClientId } from "./clientId";
import { attachmentUrl } from "./AttachmentTile";
import { FileAttachmentTile } from "./FileAttachmentTile";
import { UserMessage } from "./UserMessage";
import { ErrorDetails, ErrorNotice } from "./ErrorNotice";
import { ContentPage } from "./ContentPage";
import { ExecutionProcess } from "./ExecutionProcess";
import { MarkdownMessage } from "./MarkdownMessage";
import { formatMessageTime } from "./messageTime";
import { PendingActions } from "./PendingActions";
import { ScheduledWait } from "./ScheduledWait";
import { ThinkingBlock } from "./ThinkingBlock";
import { TurnTiming } from "./TurnTiming";
import { WorkPlanPanel } from "./WorkPlanPanel";
import { BackgroundCommands } from "./BackgroundCommands";
import { RoutineList } from "./RoutineList";
import { completedPlanForTurn, isPlanCompleted } from "./workPlanPlacement";
import { turnNeedsSubagentHint } from "./subagent";
import {
  timelineTurns,
  turnElapsedMs,
  turnIsSettled,
  turnNeedsSilentEnd,
  turnNeedsThinkingHint,
  turnReply,
  type TimelineTurn,
} from "./timelineTurns";
import { ConversationTimeline } from "./ConversationTimeline";
import { visibleTimeline } from "./visibleTimeline";
import { SubagentPanel } from "./SubagentPanel";

type OpenPage = { kind: "changes" } | { kind: "subagent"; commandId: string }
  | { kind: "error"; title: string; message: string };

export function Conversation() {
  const dispatch = useAppDispatch();
  const { sessionId: routeSessionId } = useParams();
  const routeId = routeSessionId ?? "";
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const ownerSessionId = useAppSelector((state) => state.runtime.ownerSessionId);
  const sessionId = routeId;
  const [openPage, setOpenPage] = useState<OpenPage | null>(null);
  const runtime = useAppSelector((state) => state.runtime.sessions[sessionId]);
  const selected = useGetConversationQuery(sessionId, {
    skip: routeSessionId === undefined,
  });
  const timelineRef = useRef<VirtuosoHandle>(null);
  const wheelAreaRef = useRef<HTMLDivElement>(null);
  const [following, setFollowing] = useState(true);
  const [selectSession] = useSelectSessionMutation();
  const [sendInput, sending] = useSendInputMutation();
  const [authorizeCommand, authorizing] = useAuthorizeCommandMutation();
  const [resolveControl, resolvingControl] = useResolveControlMutation();
  const [setAutoAuthorize, autoAuthorizing] = useSetAutoAuthorizeMutation();
  const [cancelTurn, stopping] = useCancelTurnMutation();
  const [retryTurn, retrying] = useRetryTurnMutation();

  useEffect(() => {
    dispatch(viewing(sessionId === "" ? null : sessionId));
    return () => {
      dispatch(viewing(null));
    };
  }, [dispatch, sessionId]);

  useEffect(() => {
    if (connectionId === null || routeSessionId === undefined) {
      return;
    }
    if (ownerSessionId === sessionId) {
      return;
    }
    void selectSession({ connectionId, sessionId });
  }, [connectionId, ownerSessionId, routeSessionId, selectSession, sessionId]);

  const conversation = selected.currentData;
  if (
    conversation === undefined &&
    (selected.isLoading || selected.isUninitialized || selected.isFetching)
  ) {
    return (
      <Center h="100%">
        <Stack align="center" gap="sm">
          <Loader size="sm" />
          <Text c="dimmed" size="sm">
            正在打开会话…
          </Text>
        </Stack>
      </Center>
    );
  }
  if (selected.isError || conversation === undefined) {
    const missing =
      selected.isError &&
      "status" in selected.error &&
      selected.error.status === 404;
    return (
      <Center h="100%" p="xl">
        <Alert color="red" icon={<IconAlertCircle size={18} />} title="无法打开会话">
          {missing ? "这个会话不存在。请从左侧新建或选择会话。" : "会话加载失败"}
        </Alert>
      </Center>
    );
  }
  const items = visibleTimeline(
    conversation,
    runtime?.committed ?? {},
    runtime?.activePreview ?? null,
    runtime?.tools ?? {},
    runtime?.committedThinking ?? {},
    runtime?.activeThinking ?? null,
    runtime?.activity ?? null,
  );
  const turns = timelineTurns(items);
  const running = runtime?.activity === "running";
  const lastError = running ? null : runtime?.lastError ?? conversation.session.last_error;
  const compactPhase =
    runtime?.conversationStatus?.compactPhase ?? conversation.compact_phase;
  const lastTurnKey = turns.at(-1)?.key;
  const awaitingControl = conversation.session.control_approval !== null;
  const notice =
    runtime?.controlNotice ?? conversation.session.control_message;
  const subagentsActive = conversation.session.has_active_subagents;
  const activePlan = conversation.work_plan !== null && !isPlanCompleted(conversation.work_plan)
    ? conversation.work_plan : null;

  async function send(text: string, artifactRefs: string[]) {
    if (connectionId === null) {
      throw new Error("Web connection is not active");
    }
    await sendInput({
      connectionId,
      sessionId,
      deliveryId: `web-${createClientId()}`,
      text,
      artifactRefs,
    }).unwrap();
    dispatch(controlNotice({ sessionId, message: null }));
  }

  async function authorize(commandId: string, approved: boolean) {
    if (connectionId === null) {
      return;
    }
    await authorizeCommand({
      connectionId,
      sessionId,
      commandId,
      approved,
    }).unwrap();
  }

  async function decideControl(approved: boolean) {
    const requestId = conversation?.session.control_approval?.request_id;
    if (connectionId === null || requestId === undefined) {
      return;
    }
    await resolveControl({ connectionId, sessionId, requestId, approved }).unwrap();
  }

  async function toggleAutoAuthorize(enabled: boolean) {
    if (connectionId === null) {
      return;
    }
    await setAutoAuthorize({ connectionId, sessionId, enabled }).unwrap();
  }

  return (
    <Box className="conversation-layout">
    <Box component="section" className="conversation">
      {items.length === 0 ? (
        <ConversationWelcome />
      ) : (
        <ConversationTimeline
          turns={turns}
          listRef={timelineRef}
          wheelAreaRef={wheelAreaRef}
          following={following}
          onFollowingChange={setFollowing}
          renderTurn={(turn) => {
              const thinking = replyThinking(turn);
              const latest = turn.key === lastTurnKey;
              const settled = turnIsSettled(turn, {
                latest,
                running,
                awaitingControl,
              });
              const reply = turnReply(turn, settled);
              const elapsedMs = turnElapsedMs(turn, settled);
              const completedPlan = completedPlanForTurn(turn, conversation.work_plan_updates);
              const plan = completedPlan === null ? null : <WorkPlanPanel plan={completedPlan} />;
              const body = (
                <>
                  <TurnTiming
                    startedAt={turn.user?.occurredAt ?? null}
                    running={latest && running && connectionId !== null}
                    elapsedMs={elapsedMs}
                  />
                  {turn.process.length === 0 ? null : (
                    <ExecutionProcess
                      authorizationDisabled={connectionId === null}
                      complete={settled}
                      sessionId={sessionId}
                      onAuthorize={authorize}
                      steps={turn.process}
                      onObserveSubagent={(commandId) => setOpenPage({ kind: "subagent", commandId })}
                    />
                  )}
                  {thinking === null ? null : (
                    <ThinkingBlock
                      streaming={thinking.thinkingPending && !settled}
                      text={thinking.thinking}
                    />
                  )}
                  {turnNeedsThinkingHint(turn, {
                    latest,
                    running,
                    settled,
                  }) ? (
                    <RunningHint label="思考中" />
                  ) : reply !== null ? (
                    <Box
                      component="article"
                      className="message message-assistant"
                      key={reply.key}
                    >
                      <Group align="flex-start" gap="sm" wrap="nowrap">
                        <img className="assistant-mark" src={redPandaMark} alt="RED PANDA" width={28} height={28} />
                        <Stack className="assistant-reply" gap={6}>
                          <MarkdownMessage
                            content={reply.text}
                            streaming={reply.streaming}
                          />
                          {reply.files.length === 0 ? null : (
                            <Group gap={8}>
                              {reply.files.map((file) => (
                                <FileAttachmentTile
                                  key={file.attachment_id}
                                  name={file.name}
                                  size={file.size}
                                  href={attachmentUrl(sessionId, file.attachment_id)}
                                />
                              ))}
                            </Group>
                          )}
                          {settled ? (
                            <TurnEndActions
                              occurredAt={turn.final?.occurredAt ?? null}
                              replyText={reply.text}
                            />
                          ) : null}
                        </Stack>
                      </Group>
                    </Box>
                  ) : turnNeedsSilentEnd(turn, settled) &&
                    !(latest && notice !== null) ? (
                    <SilentEndHint />
                  ) : null}
                </>
              );
              return (
              <Stack gap="lg" key={turn.key}>
                {turn.user === null ? null : (
                  <Box component="article" className="message message-user">
                    <UserMessage
                      images={turn.user.images}
                      files={turn.user.files}
                      sessionId={sessionId}
                      text={turn.user.text}
                      occurredAt={turn.user.occurredAt}
                    />
                  </Box>
                )}
                {body}
                {latest && notice !== null ? (
                  <ControlNotice message={notice} />
                ) : null}
                {turnNeedsSubagentHint(
                  turn.key === lastTurnKey,
                  subagentsActive,
                ) ? (
                  <RunningHint label="子 Agent 执行中" />
                ) : null}
                {turn.key === lastTurnKey
                  ? (conversation.subagent_terminals ?? []).map((terminal) => (
                    terminal.failure === null ? null : (
                      <Alert
                        key={terminal.command_id}
                        color="red"
                        icon={<IconAlertCircle size={16} />}
                        title="子 Agent 执行失败"
                      >
                        <Text className="pre-wrap" size="sm">{terminal.failure}</Text>
                      </Alert>
                    )
                  ))
                  : null}
                {plan}
              </Stack>
              );
          }}
        />
      )}
      <Box className="composer-dock" ref={wheelAreaRef}>
        {items.length === 0 || following ? null : (
          <Button
            className="jump-to-latest"
            leftSection={<IconArrowDown size={14} />}
            onClick={() => {
              setFollowing(true);
              timelineRef.current?.scrollToIndex({ index: "LAST", align: "end" });
            }}
            size="compact-sm"
            variant="default"
          >
            跳到最新
          </Button>
        )}
        <Box>
          <Box className="composer-column">
            <PendingActions
              authorizations={conversation.session.pending_authorization_commands}
              control={conversation.session.control_approval}
              authorizationDisabled={connectionId === null || authorizing.isLoading}
              controlDisabled={connectionId === null || resolvingControl.isLoading}
              onAuthorize={(commandId, approved) => void authorize(commandId, approved)}
              onControl={(approved) => void decideControl(approved)}
            />
          </Box>
          {activePlan === null ? null : (
            <Box className="composer-column" mb={8}>
              <WorkPlanPanel plan={activePlan}
                stopped={!running && (conversation.session.paused || lastError != null)} />
            </Box>
          )}
        </Box>
        <Stack className="composer-column" gap={8}>
        {conversation.waiting_until === null ? null : (
          <ScheduledWait dueAt={conversation.waiting_until} />
        )}
        <BackgroundCommands sessionId={sessionId} connectionId={connectionId} />
        <RoutineList sessionId={sessionId} connectionId={connectionId} />
        {compactPhase !== "failed" ? null : (
          <Alert
            className="composer-error"
            color="red"
            icon={<IconAlertCircle size={16} />}
            py="xs"
          >
            <Text className="pre-wrap" ff="monospace" fz={11}>
              上下文整理失败。窗口仍超预算时会话无法继续推进。
            </Text>
          </Alert>
        )}
        {lastError == null ? null : (
          <ErrorNotice title="执行已停止" message={lastError}
            onOpen={(message) => setOpenPage({ kind: "error", title: "执行已停止", message })}>
            <Button
              disabled={connectionId === null}
              loading={retrying.isLoading}
              onClick={() => {
                if (connectionId === null) {
                  return;
                }
                void retryTurn({ connectionId, sessionId }).unwrap();
              }}
              size="compact-xs"
              variant="white"
            >
              再试
            </Button>
          </ErrorNotice>
        )}
        {sending.isError || retrying.isError || stopping.isError ? (
          <ErrorNotice title="操作失败" onOpen={(message) => setOpenPage({ kind: "error", title: "操作失败", message })} message={stopping.isError
              ? requestErrorMessage(stopping.error, "停止失败，请确认后端连接后重试。")
              : retrying.isError
                ? requestErrorMessage(retrying.error, "再试失败，请确认后端连接后重试。")
                : requestErrorMessage(sending.error, "消息发送失败，请确认后端连接后重试。")} />
        ) : null}
        <FileChangesSummary sessionId={sessionId} onOpen={() => setOpenPage({ kind: "changes" })} />
        <Composer
          sessionId={sessionId}
          workspaceId={conversation.workspace_id}
          connectionId={connectionId}
          disabled={connectionId === null}
          sending={sending.isLoading}
          running={running === true}
          shouldWake={
            conversation.session.should_wake && lastError == null
          }
          stopBusy={stopping.isLoading}
          retryBusy={retrying.isLoading}
          autoAuthorize={conversation.session.auto_authorize}
          autoAuthorizeBusy={autoAuthorizing.isLoading}
          inputTokens={conversation.context_input_tokens}
          onToggleAutoAuthorize={toggleAutoAuthorize}
          onSend={send}
          onRetry={() => {
            if (connectionId === null) {
              return;
            }
            void retryTurn({ connectionId, sessionId }).unwrap();
          }}
          onStop={() => {
            if (connectionId === null) {
              return;
            }
            void cancelTurn({
              connectionId,
              sessionId,
            });
          }}
        />
        </Stack>
      </Box>
    </Box>
    {openPage === null ? null : (
      <ContentPage title={openPage.kind === "changes" ? "文件改动"
        : openPage.kind === "subagent" ? "子 Agent" : `${openPage.title} · 错误详情`}
        onClose={() => setOpenPage(null)}>
        {openPage.kind === "changes" ? <FileChangesPanel sessionId={sessionId} />
          : openPage.kind === "subagent" ? <SubagentPanel key={openPage.commandId}
            parentSessionId={sessionId} commandId={openPage.commandId} />
          : <ErrorDetails message={openPage.message} />}
      </ContentPage>
    )}
    </Box>
  );
}

function RunningHint({ label }: { label: string }) {
  return (
    <Box component="article" className="message message-assistant">
      <Group align="center" gap="sm" wrap="nowrap">
        <img className="assistant-mark" src={redPandaMark} alt="RED PANDA" width={28} height={28} />
        <Group gap={8} wrap="nowrap">
          <Loader color="ember" size={12} />
          <Text c="dimmed" size="sm">
            {label}
          </Text>
        </Group>
      </Group>
    </Box>
  );
}

function TurnEndActions({
  replyText,
  occurredAt,
}: {
  replyText: string | null;
  occurredAt: string | null;
}) {
  const [copied, setCopied] = useState(false);
  const time = occurredAt === null ? null : formatMessageTime(occurredAt);
  return (
    <Group className="turn-end-actions" gap={4} justify="flex-start">
      {replyText === null ? null : (
        <ActionIcon
          aria-label="复制回复"
          onClick={() => {
            void navigator.clipboard.writeText(replyText).then(() => {
              setCopied(true);
              window.setTimeout(() => setCopied(false), 1500);
            });
          }}
          radius="xl"
          size="sm"
          variant="subtle"
        >
          {copied ? <IconCheck size={14} /> : <IconCopy size={14} />}
        </ActionIcon>
      )}
      {time === null ? null : (
        <Text component="time" dateTime={occurredAt ?? undefined} c="dimmed" fz={11} ml={4} title={`回复提交于 ${time.full}`}>
          {time.short}
        </Text>
      )}
    </Group>
  );
}

function SilentEndHint() {
  return (
    <Box component="article" className="message message-assistant">
      <Group align="center" gap="sm" wrap="nowrap">
        <ThemeIcon radius="xl" size={28} variant="subtle">
          <IconSparkles size={15} />
        </ThemeIcon>
        <Text c="dimmed" size="sm">
          这一轮没有文字回复
        </Text>
      </Group>
    </Box>
  );
}

function ControlNotice({ message }: { message: string }) {
  return (
    <Alert
      className="control-notice"
      color="ember"
      icon={<IconCheck size={16} />}
      title="操作结果"
      variant="light"
    >
      <Text className="pre-wrap" size="sm">
        {message}
      </Text>
    </Alert>
  );
}

function replyThinking(turn: TimelineTurn) {
  const step = turn.active ?? turn.final;
  if (step === null || step.thinking === null) {
    return null;
  }
  return { thinking: step.thinking, thinkingPending: step.thinkingPending };
}

function requestErrorMessage(error: unknown, fallback: string) {
  if (typeof error !== "object" || error === null || !("data" in error)) {
    return fallback;
  }
  const data = error.data;
  if (typeof data !== "object" || data === null || !("detail" in data)) {
    return fallback;
  }
  return typeof data.detail === "string" ? data.detail : fallback;
}
