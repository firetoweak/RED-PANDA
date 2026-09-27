import {
  ActionIcon,
  Alert,
  Box,
  Button,
  Center,
  Group,
  Loader,
  Modal,
  ScrollArea,
  Stack,
  Text,
  ThemeIcon,
  Title,
} from "@mantine/core";
import {
  IconAlertCircle,
  IconArrowDown,
  IconCheck,
  IconCopy,
  IconGitBranch,
  IconMessageCircle,
  IconSparkles,
  IconX,
} from "@tabler/icons-react";
import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import {
  useAuthorizeCommandMutation,
  useBranchAfterTurnMutation,
  useEditAndForkMutation,
  useGetConversationQuery,
  useResolveControlMutation,
  useSelectSessionMutation,
  useSendInputMutation,
  useSetAutoAuthorizeMutation,
  useSetPausedMutation,
  useRetryTurnMutation,
  useRestartFromStepMutation,
} from "../../api/helpermeApi";
import { useAppDispatch, useAppSelector } from "../../app/hooks";
import {
  authorizationResolved,
  controlNotice,
  lockDraft,
  viewing,
} from "../../realtime/runtimeSlice";
import { Composer } from "./Composer";
import { EditableUserMessage } from "./EditableUserMessage";
import { ExecutionProcess } from "./ExecutionProcess";
import { MarkdownMessage } from "./MarkdownMessage";
import { ScheduledWait } from "./ScheduledWait";
import { ThinkingBlock } from "./ThinkingBlock";
import { turnNeedsSubagentHint } from "./subagent";
import {
  timelineTurns,
  turnCanStartSession,
  turnIsSettled,
  turnNeedsSilentEnd,
  turnNeedsThinkingHint,
  turnReply,
  type TimelineTurn,
} from "./timelineTurns";
import { useFollowOutput } from "./useFollowOutput";
import { visibleTimeline } from "./visibleTimeline";

export function Conversation() {
  const dispatch = useAppDispatch();
  const navigate = useNavigate();
  const { sessionId: routeSessionId } = useParams();
  const routeId = routeSessionId ?? "";
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const ownerSessionId = useAppSelector((state) => state.runtime.ownerSessionId);
  const draftSessions = useAppSelector((state) => state.runtime.draftSessions);
  const sessionId = routeId;
  const runtime = useAppSelector((state) => state.runtime.sessions[sessionId]);
  const selected = useGetConversationQuery(sessionId, {
    skip: routeSessionId === undefined,
  });
  const followOutput = useFollowOutput(
    routeSessionId,
    runtime?.activity === "running" ||
      (runtime?.activePreview ?? null) !== null ||
      (runtime?.activeThinking ?? null) !== null,
    selected.currentData !== undefined,
  );
  const [selectSession] = useSelectSessionMutation();
  const [sendInput, sending] = useSendInputMutation();
  const [editAndFork, editing] = useEditAndForkMutation();
  const [branchAfterTurn, branching] = useBranchAfterTurnMutation();
  const [authorizeCommand, authorizing] = useAuthorizeCommandMutation();
  const [resolveControl, resolvingControl] = useResolveControlMutation();
  const [setAutoAuthorize, autoAuthorizing] = useSetAutoAuthorizeMutation();
  const [setPaused, pausing] = useSetPausedMutation();
  const [retryTurn, retrying] = useRetryTurnMutation();
  const [restartFromStep, restarting] = useRestartFromStepMutation();

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
  useEffect(() => {
    if (
      conversation !== undefined &&
      Object.values(draftSessions).includes(conversation.session_id) &&
      conversation.items.some((item) => item.kind === "user")
    ) {
      dispatch(lockDraft(conversation.session_id));
    }
  }, [conversation, dispatch, draftSessions]);
  useEffect(() => {
    if (
      routeSessionId !== undefined &&
      selected.isError &&
      "status" in selected.error &&
      selected.error.status === 404
    ) {
      dispatch(lockDraft(routeSessionId));
    }
  }, [dispatch, routeSessionId, selected.error, selected.isError]);
  if (
    conversation === undefined &&
    (selected.isLoading || selected.isUninitialized || selected.isFetching)
  ) {
    return (
      <Center h="100%">
        <Stack align="center" gap="sm">
          <Loader size="sm" />
          <Text c="dimmed" size="sm">
            正在打开 Session…
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
          {missing ? "这个 Session 不存在。请从左侧新建或选择会话。" : "Session 加载失败"}
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
  const compactCount =
    runtime?.conversationStatus?.compactCount ?? conversation.compact_count;
  const compactPhase =
    runtime?.conversationStatus?.compactPhase ?? conversation.compact_phase;
  const lastTurnKey = turns.at(-1)?.key;
  const awaitingControl = conversation.session.control_approval !== null;
  const notice =
    runtime?.controlNotice ?? conversation.session.control_message;
  const subagentsActive = conversation.session.has_active_subagents;

  async function send(text: string, artifactRefs: string[]) {
    if (connectionId === null) {
      throw new Error("Web connection is not active");
    }
    await sendInput({
      connectionId,
      sessionId,
      deliveryId: `web-${crypto.randomUUID()}`,
      text,
      artifactRefs,
    }).unwrap();
    dispatch(controlNotice({ sessionId, message: null }));
    dispatch(lockDraft(sessionId));
  }

  async function edit(
    messageId: string,
    text: string,
    restoreFiles: boolean,
  ) {
    if (connectionId === null) {
      throw new Error("Web connection is not active");
    }
    const view = await editAndFork({
      connectionId,
      sessionId,
      messageId,
      deliveryId: `web-${crypto.randomUUID()}`,
      text,
      listed: false,
      restoreFiles,
    }).unwrap();
    navigate(`/sessions/${encodeURIComponent(view.session_id)}`, {
      replace: true,
    });
  }

  async function branch(messageId: string) {
    if (connectionId === null) {
      return;
    }
    try {
      const view = await branchAfterTurn({
        connectionId,
        sessionId,
        messageId,
      }).unwrap();
      navigate(`/sessions/${encodeURIComponent(view.session_id)}`);
    } catch {
      // 错误画在输入框上方，不把 Promise 拒绝甩到控制台。
    }
  }

  async function restart(stepId: string) {
    if (connectionId === null) {
      return;
    }
    const view = await restartFromStep({
      connectionId,
      sessionId,
      stepId,
      deliveryId: `web-${crypto.randomUUID()}`,
    }).unwrap();
    // 原地改写：新身份顶掉旧的，后退不该停在一个已经不代表这条线的 URL 上。
    navigate(`/sessions/${encodeURIComponent(view.session_id)}`, { replace: true });
  }

  async function authorize(commandId: string, approved: boolean) {
    if (connectionId === null) {
      return;
    }
    try {
      await authorizeCommand({
        connectionId,
        sessionId,
        commandId,
        approved,
      }).unwrap();
    } finally {
      dispatch(authorizationResolved({ sessionId, commandId }));
    }
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

  const pendingAuthorizations = Object.values(runtime?.authorizations ?? {});
  const activeAuthorization = pendingAuthorizations[0];

  return (
    <Box component="section" className="conversation">
      {items.length === 0 ? (
        <Center className="conversation-intro">
          <Stack align="center" gap="sm" ta="center">
            <ThemeIcon radius="xl" size={48} variant="light">
              <IconMessageCircle size={23} stroke={1.7} />
            </ThemeIcon>
            <Title order={1} fz={24} fw={650}>
              开始新的会话
            </Title>
            <Text c="dimmed" ff="monospace" fz={11}>
              {shortId(sessionId)}
            </Text>
          </Stack>
        </Center>
      ) : (
        <ScrollArea
          className="timeline-scroll"
          offsetScrollbars
          type="hover"
          viewportRef={followOutput.viewportRef}
        >
          <Stack className="timeline" gap="lg" ref={followOutput.contentRef}>
            {turns.map((turn) => {
              const thinking = replyThinking(turn);
              const latest = turn.key === lastTurnKey;
              const settled = turnIsSettled(turn, {
                latest,
                running,
                awaitingControl,
              });
              const reply = turnReply(turn, settled);
              return (
              <Stack gap="lg" key={turn.key}>
                {turn.user === null ? null : (
                  <Box component="article" className="message message-user">
                    <EditableUserMessage
                      disabled={connectionId === null}
                      images={turn.user.images}
                      files={turn.user.files}
                      hasLaterWork={turn.key !== lastTurnKey || turn.process.length > 0}
                      onSave={(text, restoreFiles) =>
                        edit(turn.user!.key, text, restoreFiles)
                      }
                      saving={editing.isLoading}
                      sessionId={sessionId}
                      text={turn.user.text}
                    />
                  </Box>
                )}
                {turn.process.length === 0 ? null : (
                  <ExecutionProcess
                    authorizationDisabled={connectionId === null}
                    complete={settled}
                    onAuthorize={authorize}
                    onRestart={(stepId) => void restart(stepId)}
                    restartDisabled={connectionId === null || restarting.isLoading}
                    steps={turn.process}
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
                      <ThemeIcon radius="xl" size={28} variant="subtle">
                        <IconSparkles size={15} />
                      </ThemeIcon>
                      <Stack className="assistant-reply" gap={6}>
                        <MarkdownMessage
                          content={reply.text}
                          streaming={reply.streaming}
                        />
                        {settled ? (
                          <TurnEndActions
                            branchBusy={branching.isLoading}
                            branchDisabled={connectionId === null}
                            canBranch={turnCanStartSession(turn, settled)}
                            onBranch={() => void branch(turn.user!.key)}
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
                {latest && notice !== null ? (
                  <ControlNotice message={notice} />
                ) : null}
                {turnNeedsSubagentHint(
                  turn.key === lastTurnKey,
                  subagentsActive,
                ) ? (
                  <RunningHint label="子 Agent 执行中" />
                ) : null}
                {settled &&
                reply === null &&
                turnCanStartSession(turn, settled) ? (
                  <TurnEndActions
                    branchBusy={branching.isLoading}
                    branchDisabled={connectionId === null}
                    canBranch
                    onBranch={() => void branch(turn.user!.key)}
                    replyText={null}
                  />
                ) : null}
              </Stack>
              );
            })}
          </Stack>
        </ScrollArea>
      )}
      <Box className="composer-dock">
        {items.length === 0 || followOutput.following ? null : (
          <Button
            className="jump-to-latest"
            leftSection={<IconArrowDown size={14} />}
            onClick={followOutput.scrollToBottom}
            size="compact-sm"
            variant="default"
          >
            跳到最新
          </Button>
        )}
        <Stack className="composer-column" gap={8}>
        {conversation.workspace_version === null ? null : (
          <Text
            className="composer-meta"
            size="xs"
            c={conversation.workspace_version.error === null ? "dimmed" : "red"}
            title={conversation.workspace_version.version ?? undefined}
          >
            {conversation.workspace_version.error === null
              ? `文件版本 ${conversation.workspace_version.version?.slice(0, 12)} · 可在对话中要求回退`
              : `文件版本未记录：${conversation.workspace_version.error}`}
          </Text>
        )}
        {conversation.waiting_until === null ? null : (
          <ScheduledWait dueAt={conversation.waiting_until} />
        )}
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
        {runtime?.lastError == null ? null : (
          <Alert
            className="composer-error"
            color="red"
            icon={<IconAlertCircle size={16} />}
            py="xs"
          >
            <Group gap="sm" justify="space-between" wrap="nowrap">
              <Text className="pre-wrap" ff="monospace" fz={11}>
                {runtime.lastError}
              </Text>
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
            </Group>
          </Alert>
        )}
        {sending.isError || editing.isError || retrying.isError || branching.isError ? (
          <Alert
            className="composer-error"
            color="red"
            icon={<IconAlertCircle size={16} />}
            py="xs"
          >
            {branching.isError
              ? requestErrorMessage(
                  branching.error,
                  "未能从这一轮建立新会话。",
                )
              : editing.isError
              ? requestErrorMessage(
                  editing.error,
                  "消息编辑失败，未能改写这条消息。",
                )
              : retrying.isError
                ? requestErrorMessage(retrying.error, "再试失败，请确认后端连接后重试。")
                : "消息发送失败，请确认后端连接后重试。"}
          </Alert>
        ) : null}
        <Composer
          sessionId={sessionId}
          workspaceId={conversation.workspace_id}
          connectionId={connectionId}
          disabled={connectionId === null}
          sending={sending.isLoading}
          running={running === true}
          paused={conversation.session.paused}
          shouldWake={
            conversation.session.should_wake && runtime?.lastError == null
          }
          pauseBusy={pausing.isLoading}
          retryBusy={retrying.isLoading}
          autoAuthorize={conversation.session.auto_authorize}
          autoAuthorizeBusy={autoAuthorizing.isLoading}
          compactCount={compactCount}
          compactPhase={compactPhase}
          onToggleAutoAuthorize={toggleAutoAuthorize}
          onSend={send}
          onRetry={() => {
            if (connectionId === null) {
              return;
            }
            void retryTurn({ connectionId, sessionId }).unwrap();
          }}
          onSetPaused={(nextPaused) => {
            if (connectionId === null) {
              return;
            }
            void setPaused({
              connectionId,
              sessionId,
              paused: nextPaused,
            }).unwrap();
          }}
        />
        </Stack>
      </Box>
      <Modal
        centered
        closeOnClickOutside={false}
        closeOnEscape={false}
        onClose={() => {}}
        opened={conversation.session.control_approval !== null}
        title="等待确认"
        withCloseButton={false}
      >
        {conversation.session.control_approval === null ? null : (
          <Stack gap="md">
            <Text className="pre-wrap" fz={13}>
              {conversation.session.control_approval?.summary}
            </Text>
            <Text c="dimmed" className="pre-wrap" fz={12}>
              风险：{conversation.session.control_approval?.risk}
            </Text>
            <Group justify="flex-end" gap="xs">
              <Button
                color="gray"
                disabled={resolvingControl.isLoading}
                leftSection={<IconX size={14} />}
                onClick={() => void decideControl(false)}
              >
                取消
              </Button>
              <Button
                color="sage"
                disabled={resolvingControl.isLoading}
                leftSection={<IconCheck size={14} />}
                loading={resolvingControl.isLoading}
                onClick={() => void decideControl(true)}
              >
                确认
              </Button>
            </Group>
          </Stack>
        )}
      </Modal>
      <Modal
        centered
        closeOnClickOutside={false}
        onClose={() => {
          if (activeAuthorization !== undefined) {
            dispatch(
              authorizationResolved({
                sessionId,
                commandId: activeAuthorization.commandId,
              }),
            );
          }
        }}
        opened={activeAuthorization !== undefined}
        title="等待授权"
      >
        {activeAuthorization === undefined ? null : (
          <Stack gap="md">
            <Text ff="monospace" fw={600}>
              {activeAuthorization.name}
            </Text>
            <Text c="dimmed" className="pre-wrap" ff="monospace" fz={12}>
              {JSON.stringify(activeAuthorization.arguments, null, 2)}
            </Text>
            <Group justify="flex-end" gap="xs">
              <Button
                color="gray"
                disabled={connectionId === null || authorizing.isLoading}
                leftSection={<IconX size={14} />}
                onClick={() => authorize(activeAuthorization.commandId, false)}
              >
                拒绝
              </Button>
              <Button
                color="sage"
                disabled={connectionId === null || authorizing.isLoading}
                leftSection={<IconCheck size={14} />}
                loading={authorizing.isLoading}
                onClick={() => authorize(activeAuthorization.commandId, true)}
              >
                允许
              </Button>
            </Group>
          </Stack>
        )}
      </Modal>
    </Box>
  );
}

function RunningHint({ label }: { label: string }) {
  return (
    <Box component="article" className="message message-assistant">
      <Group align="center" gap="sm" wrap="nowrap">
        <ThemeIcon radius="xl" size={28} variant="subtle">
          <IconSparkles size={15} />
        </ThemeIcon>
        <Group gap={8} wrap="nowrap">
          <Loader color="sage" size={12} />
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
  canBranch,
  branchBusy,
  branchDisabled,
  onBranch,
}: {
  replyText: string | null;
  canBranch: boolean;
  branchBusy: boolean;
  branchDisabled: boolean;
  onBranch: () => void;
}) {
  const [copied, setCopied] = useState(false);
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
      {canBranch ? (
        <ActionIcon
          aria-label="从这一轮之后建立新会话"
          disabled={branchDisabled}
          loading={branchBusy}
          onClick={onBranch}
          radius="xl"
          size="sm"
          variant="subtle"
        >
          <IconGitBranch size={14} />
        </ActionIcon>
      ) : null}
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
      color="sage"
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

function shortId(id: string) {
  return `Session · ${id.slice(-8)}`;
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
