import {
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
  IconSparkles,
} from "@tabler/icons-react";
import { useCallback, useEffect, useRef, useState } from "react";
import type { VirtuosoHandle } from "react-virtuoso";
import { useParams } from "react-router-dom";

import {
  useAuthorizeCommandMutation,
  useCancelTurnMutation,
  useGetConversationQuery,
  useResolveControlMutation,
  useSelectSessionMutation,
  useSendInputMutation,
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
import { SessionAttention } from "./SessionAttention";
import { ScheduledWait } from "./ScheduledWait";
import { ThinkingBlock } from "./ThinkingBlock";
import { WorkPlanPanel } from "./WorkPlanPanel";
import { BackgroundCommands } from "./BackgroundCommands";
import { completedPlanForTurn, isPlanCompleted } from "./workPlanPlacement";
import { turnNeedsSubagentHint } from "./subagent";
import {
  timelineTurns,
  turnIsSettled,
  turnNeedsSilentEnd,
  turnNeedsThinkingHint,
  turnReply,
  type TimelineTurn,
} from "./timelineTurns";
import { ConversationTimeline } from "./ConversationTimeline";
import { registerTimelineFocus } from "./timelineFocus";
import { visibleTimeline } from "./visibleTimeline";
import { SubagentPanel } from "./SubagentPanel";
import { BotAvatar, ChatPresentation, MessageMenu, type MessageReference } from "./ChatPresentation";

const privateReading = new Map<string, { following: boolean; count: number }>();

type OpenPage = { kind: "changes" } | { kind: "subagent"; commandId: string }
  | { kind: "error"; title: string; message: string };

function turnHasOutput(turn: TimelineTurn, outputId: string): boolean {
  return [turn.active, turn.final, ...turn.process].some((step) => step?.outputId === outputId);
}

export function Conversation({ agentName, agentNames = [] }: { agentName?: string; agentNames?: string[] }) {
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
  const focusRef = useRef<(outputId: string) => void>(() => {});
  const [following, setFollowingState] = useState(privateReading.get(sessionId)?.following ?? true);
  const [quote, setQuote] = useState<MessageReference | null>(null);
  const [quoteReturn, setQuoteReturn] = useState<number | null>(null);
  const jumpRef = useRef<(id: string) => void>(() => {});
  const readCount = useRef(privateReading.get(sessionId)?.count ?? null);
  const messageKeys = (selected.currentData?.items ?? [])
    .filter((item) => item.kind === "user" || (item.text !== null && item.tools.length === 0))
    .map((item) => item.kind === "user" ? item.message_id : `output:${item.output_id}`);
  const count = messageKeys.length;
  const [newCount, setNewCount] = useState(0);
  const setFollowing = useCallback((value: boolean) => {
    setFollowingState(value);
    const old = privateReading.get(sessionId);
    privateReading.set(sessionId, { following: value, count: old?.count ?? 0 });
  }, [sessionId]);
  useEffect(() => {
    if (selected.currentData === undefined) return;
    if (following || readCount.current === null) readCount.current = count;
    setNewCount(Math.max(0, count - readCount.current));
    privateReading.set(sessionId, { following, count: readCount.current });
  }, [count, following, selected.currentData, sessionId]);
  const [highlightedKey, setHighlightedKey] = useState<string | null>(null);
  const [quotedMessage, setQuotedMessage] = useState<string | null>(null);
  const [selectSession] = useSelectSessionMutation();
  const [sendInput, sending] = useSendInputMutation();
  const [authorizeCommand, authorizing] = useAuthorizeCommandMutation();
  const [resolveControl, resolvingControl] = useResolveControlMutation();
  const [cancelTurn, stopping] = useCancelTurnMutation();
  const [retryTurn, retrying] = useRetryTurnMutation();

  useEffect(() => {
    dispatch(viewing(sessionId === "" ? null : sessionId));
    return () => {
      dispatch(viewing(null));
    };
  }, [dispatch, sessionId]);

  useEffect(() => registerTimelineFocus((outputId) => focusRef.current(outputId)), []);

  useEffect(() => {
    if (highlightedKey === null && quotedMessage === null) return;
    const timer = window.setTimeout(() => { setHighlightedKey(null); setQuotedMessage(null); }, 2400);
    return () => window.clearTimeout(timer);
  }, [highlightedKey, quotedMessage]);

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
  const firstUnread = newCount === 0 ? undefined : messageKeys.at(-newCount);
  jumpRef.current = (id) => {
    const index = turns.findIndex((turn) => turn.user?.key === id || turnReply(turn, true)?.key === id);
    if (index < 0) return;
    if (quoteReturn === null) timelineRef.current?.getState((state) => setQuoteReturn(state.scrollTop));
    setFollowing(false);
    setQuotedMessage(id);
    timelineRef.current?.scrollIntoView({ index, align: "start",
      calculateViewLocation: () => ({ index, align: "start" }),
      done: () => requestAnimationFrame(() => {
        const viewport = wheelAreaRef.current?.parentElement?.querySelector<HTMLElement>(".virtual-timeline");
        const target = Array.from(viewport?.querySelectorAll<HTMLElement>("[data-message-id]") ?? [])
          .find((message) => message.dataset.messageId === id);
        if (viewport !== null && viewport !== undefined && target !== undefined)
          viewport.scrollTop += target.getBoundingClientRect().top - viewport.getBoundingClientRect().top - 20;
      }),
    });
  };
  focusRef.current = (outputId: string) => {
    const index = turns.findIndex((turn) => turnHasOutput(turn, outputId));
    if (index < 0) return;
    setFollowing(false);
    setHighlightedKey(turns[index].key);
    timelineRef.current?.scrollToIndex({ index, align: "center" });
  };
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

  return (
    <ChatPresentation value={{ names: agentNames.length > 0 ? agentNames : agentName === undefined ? [] : [agentName], quote, onQuote: setQuote, onJump: (id) => jumpRef.current(id) }}>
    <Box className="conversation-layout">
    <Box component="section" className="conversation">
      {items.length === 0 ? (
        <ConversationWelcome />
      ) : (
        <ConversationTimeline
          conversationKey={sessionId}
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
              const completedPlan = completedPlanForTurn(turn, conversation.work_plan_updates);
              const plan = completedPlan === null ? null : <WorkPlanPanel plan={completedPlan} />;
              const body = (
                <div className="assistant-turn">
                  {turnNeedsThinkingHint(turn, {
                    latest,
                    running,
                    settled,
                  }) ? (
                    <RunningHint label="思考中" agentName={agentName} />
                  ) : reply !== null ? (
                    <>
                    {firstUnread !== undefined && reply.key === firstUnread ? <div className="unread-divider" role="separator">以下是未读消息</div> : null}
                    <Box
                      component="article"
                      className={`message message-assistant${quotedMessage === reply.key ? " is-quote-target" : ""}`}
                      data-message-id={reply.key}
                      aria-label={`${agentName ?? "RED PANDA"} 的回复`}
                      key={reply.key}
                    >
                        <AssistantAvatar name={agentName} />
                        <MessageMenu id={reply.key} author={agentName ?? "RED PANDA"} text={reply.text}>
                        <Stack className="assistant-reply" gap={6} tabIndex={0}>
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
                        </Stack>
                        </MessageMenu>
                    </Box>
                    </>
                  ) : turnNeedsSilentEnd(turn, settled) &&
                    !(latest && notice !== null) ? (
                    <SilentEndHint />
                  ) : null}
                  {!(reply !== null && settled) && turn.process.length === 0 && thinking === null ? null : <div className="turn-details" aria-label="消息操作与过程">
                  {turn.process.length === 0 ? null : (
                    <ExecutionProcess
                      authorizationDisabled={connectionId === null}
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
                  {reply !== null && settled && turn.final?.occurredAt != null ? (
                    <Text className="message-time" component="time" dateTime={turn.final.occurredAt} c="dimmed" fz={11}
                      title={`回复提交于 ${formatMessageTime(turn.final.occurredAt).full}`}>
                      {formatMessageTime(turn.final.occurredAt).short} · 已发送
                    </Text>
                  ) : null}
                  </div>}
                </div>
              );
              return (
              <Stack className={turn.key === highlightedKey ? "conversation-turn is-focused" : "conversation-turn"} gap={24} key={turn.key}>
                {firstUnread !== undefined && turn.user?.key === firstUnread ? <div className="unread-divider" role="separator">以下是未读消息</div> : null}
                {turn.user === null ? null : (
                  <Box component="article" className={`message message-user${quotedMessage === turn.user.key ? " is-quote-target" : ""}`} data-message-id={turn.user.key}>
                    <span className="agent-avatar human-avatar" aria-hidden="true">你</span>
                    <UserMessage
                      messageId={turn.user.key}
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
        {quoteReturn !== null ? <Button className="jump-to-latest" variant="default" size="compact-sm" onClick={() => { timelineRef.current?.scrollTo({ top: quoteReturn }); setQuoteReturn(null); }}>返回刚才的位置</Button> : items.length === 0 || following ? null : (
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
            {newCount > 0 ? `${newCount} 条新消息` : "跳到最新"}
          </Button>
        )}
        <Box>
          <Box className="composer-column">
            {agentName === undefined ? null : <SessionAttention key={sessionId} sessionId={sessionId} connectionId={connectionId} />}
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
        {conversation.workspace_id === null ? null : (
          <FileChangesSummary sessionId={sessionId} onOpen={() => setOpenPage({ kind: "changes" })} />
        )}
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
          inputTokens={conversation.context_input_tokens}
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
    </ChatPresentation>
  );
}

function AssistantAvatar({ name }: { name?: string }) {
  return name === undefined
    ? <img className="assistant-mark" src={redPandaMark} alt="RED PANDA" width={28} height={28} />
    : <BotAvatar name={name} />;
}

function RunningHint({ label, agentName }: { label: string; agentName?: string }) {
  return (
    <Box component="article" className="message message-assistant">
      <Group align="center" gap="sm" wrap="nowrap">
        <AssistantAvatar name={agentName} />
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
