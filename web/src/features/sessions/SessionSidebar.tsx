import {
  ActionIcon,
  AppShell,
  Badge,
  Box,
  Button,
  Collapse,
  Group,
  ScrollArea,
  Skeleton,
  Stack,
  Text,
  TextInput,
  Tooltip,
  UnstyledButton,
} from "@mantine/core";
import {
  IconArchive,
  IconFolder,
  IconFolderPlus,
  IconMessageCircle,
  IconPencil,
  IconPlus,
  IconAdjustments,
} from "@tabler/icons-react";
import { useDisclosure } from "@mantine/hooks";
import { useEffect, useState } from "react";
import { useMatch, useNavigate } from "react-router-dom";

import type { SessionSummary } from "../../api/contracts";
import {
  useArchiveSessionMutation,
  useGetSessionsQuery,
  useGetSessionTitlesQuery,
  useGetWorkspacesQuery,
  useSetSessionTitleMutation,
} from "../../api/redpandaApi";
import { useAppDispatch, useAppSelector } from "../../app/hooks";
import { lockDraft } from "../../realtime/runtimeSlice";
import { redPandaMark } from "../../app/brand";
import { ColorSchemeSwitcher } from "../../app/ColorSchemeSwitcher";
import { CreateWorkspaceModal } from "./CreateWorkspaceModal";
import {
  DRAFT_TITLE,
  draftSessionId,
  defaultWorkspaceId,
  formatRelativeTime,
  groupSessions,
  readCollapsedWorkspaces,
  workspaceOfSession,
  workspaceSessionRows,
  writeCollapsedWorkspaces,
} from "./workspaces";

type SessionSidebarProps = {
  onNavigate: () => void;
};

export function SessionSidebar({ onNavigate }: SessionSidebarProps) {
  const navigate = useNavigate();
  const sessionId = useMatch("/sessions/:sessionId")?.params.sessionId;
  const modelSettingsActive = useMatch("/settings/models") !== null;
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const draftSessions = useAppSelector((state) => state.runtime.draftSessions);
  const [expandedWorkspaces, setExpandedWorkspaces] = useState<
    Record<string, boolean>
  >({});
  const { data: sessions = [], isLoading } = useGetSessionsQuery();
  const { data: titles = {} } = useGetSessionTitlesQuery();
  const { data: workspaces = [], isLoading: workspacesLoading } =
    useGetWorkspacesQuery();
  const [
    createWorkspaceOpened,
    { open: openCreateWorkspace, close: closeCreateWorkspace },
  ] = useDisclosure(false);
  const [collapsedWorkspaces, setCollapsedWorkspaces] = useState(
    readCollapsedWorkspaces,
  );
  const workspaceGroups = groupSessions(sessions, workspaces);
  const fallbackWorkspaceId = defaultWorkspaceId(workspaceGroups, workspaces);
  const currentWorkspaceId = workspaceOfSession(
    sessionId,
    sessions,
    draftSessions,
  );
  const defaultDraftId = draftSessionId(draftSessions, fallbackWorkspaceId);
  const canCreateSession =
    connectionId !== null && workspaces.length > 0;

  function toggleWorkspace(workspaceId: string) {
    setCollapsedWorkspaces((current) => {
      const next = { ...current, [workspaceId]: !current[workspaceId] };
      writeCollapsedWorkspaces(next);
      return next;
    });
  }

  function openDraft(workspaceId?: string) {
    const targetId = workspaceId ?? fallbackWorkspaceId;
    if (targetId === null) {
      return;
    }
    setCollapsedWorkspaces((current) => {
      if (current[targetId] !== true) {
        return current;
      }
      const next = { ...current, [targetId]: false };
      writeCollapsedWorkspaces(next);
      return next;
    });
    const existing = draftSessionId(draftSessions, targetId);
    if (sessionId !== undefined && sessionId === existing) {
      onNavigate();
      return;
    }
    navigate(`/workspaces/${encodeURIComponent(targetId)}`);
    onNavigate();
  }

  return (
    <Stack h="100%" gap="md">
      <AppShell.Section>
        <Group className="sidebar-brand" gap="sm" px={6} py={4} wrap="nowrap">
          <img className="brand-mark" src={redPandaMark} alt="" width={40} height={40} />
          <Box>
            <Text className="brand-name" fw={700} lh={1.15} size="sm">
              RED PANDA
            </Text>
            <Text c="dimmed" fz={11}>
              你的个人助手
            </Text>
          </Box>
        </Group>
      </AppShell.Section>

      <AppShell.Section>
        <Button
          fullWidth
          justify="flex-start"
          leftSection={<IconPlus size={17} />}
          disabled={!canCreateSession}
          onClick={() => openDraft()}
          radius="md"
          variant={sessionId === defaultDraftId ? "filled" : "light"}
        >
          新建会话
        </Button>
      </AppShell.Section>

      <AppShell.Section grow className="session-section">
        <Group gap={4} justify="space-between" px={8} wrap="nowrap">
          <Text c="dimmed" fw={700} fz={10} lts="0.09em" tt="uppercase">
            工作区
          </Text>
          <Tooltip label="新建工作区" openDelay={400} position="left">
            <ActionIcon
              aria-label="新建工作区"
              onClick={openCreateWorkspace}
              size="sm"
              variant="subtle"
            >
              <IconFolderPlus size={15} />
            </ActionIcon>
          </Tooltip>
        </Group>
        <ScrollArea className="session-scroll" type="hover">
          {isLoading ? (
            <Stack gap={8} mt="xs" px={6}>
              <Skeleton h={38} radius="md" />
              <Skeleton h={38} radius="md" />
              <Skeleton h={38} radius="md" />
            </Stack>
          ) : null}
          <Stack gap={4} mt="xs">
            {!workspacesLoading && workspaces.length === 0 ? (
              <Box px={8} py="sm">
                <Text c="dimmed" fz={12}>
                  添加一个工作区，整理会话与相关文件。
                </Text>
                <Button
                  fullWidth
                  mt={8}
                  onClick={openCreateWorkspace}
                  size="xs"
                  variant="light"
                >
                  新建工作区
                </Button>
              </Box>
            ) : null}
            <CreateWorkspaceModal
              onClose={closeCreateWorkspace}
              opened={createWorkspaceOpened}
            />
            {workspaceGroups.map((workspace) => {
              const collapsed = collapsedWorkspaces[workspace.id] === true;
              const expanded = expandedWorkspaces[workspace.id] === true;
              const draftId = draftSessions[workspace.id];
              const { shown: shownSessions, hiddenCount } = workspaceSessionRows(
                workspace.sessions,
                workspace.id,
                draftId,
                expanded,
                titles[draftId ?? ""] ?? DRAFT_TITLE,
              );
              return (
                <Box
                  className={
                    workspace.id === currentWorkspaceId
                      ? "workspace-group is-current"
                      : "workspace-group"
                  }
                  key={workspace.id}
                >
                  <Group className="workspace-head" gap={0} wrap="nowrap">
                    <UnstyledButton
                      aria-expanded={!collapsed}
                      className="workspace-toggle"
                      onClick={() => toggleWorkspace(workspace.id)}
                    >
                      <Group gap={6} wrap="nowrap">
                        <IconFolder size={14} />
                        <Text
                          className="workspace-name"
                          fw={600}
                          fz={12}
                          truncate
                        >
                          {workspace.name}
                        </Text>
                      </Group>
                    </UnstyledButton>
                    <Tooltip
                      label="在此工作区新建会话"
                      openDelay={400}
                      position="right"
                    >
                      <ActionIcon
                        aria-label={`在 ${workspace.name} 新建会话`}
                        className="workspace-add"
                        disabled={connectionId === null}
                        onClick={() => openDraft(workspace.id)}
                        size="sm"
                        variant="subtle"
                      >
                        <IconPlus size={14} />
                      </ActionIcon>
                    </Tooltip>
                  </Group>
                  <Collapse expanded={!collapsed}>
                    <Stack className="workspace-sessions" gap={3}>
                      {shownSessions.map((session) => (
                        <SessionRow
                          key={session.session_id}
                          active={session.session_id === sessionId}
                          connectionId={connectionId}
                          onOpen={() => {
                            navigate(
                              `/sessions/${encodeURIComponent(session.session_id)}`,
                            );
                            onNavigate();
                          }}
                          onArchivedCurrent={() => {
                            navigate(
                              `/workspaces/${encodeURIComponent(workspace.id)}`,
                            );
                            onNavigate();
                          }}
                          session={{
                            ...session,
                            title:
                              titles[session.session_id] ?? session.title,
                          }}
                        />
                      ))}
                      {hiddenCount > 0 ? (
                        <UnstyledButton
                          className="workspace-more"
                          onClick={() =>
                            setExpandedWorkspaces((current) => ({
                              ...current,
                              [workspace.id]: true,
                            }))
                          }
                        >
                          <Text c="dimmed" fz={12}>
                            更多 · {hiddenCount}
                          </Text>
                        </UnstyledButton>
                      ) : null}
                    </Stack>
                  </Collapse>
                </Box>
              );
            })}
          </Stack>
        </ScrollArea>
      </AppShell.Section>

      <AppShell.Section className="sidebar-footer">
        <Group gap={8} wrap="nowrap">
          <Button
            className="sidebar-settings" variant={modelSettingsActive ? "light" : "subtle"}
            justify="flex-start" leftSection={<IconAdjustments size={17} />}
            onClick={() => { navigate("/settings/models"); onNavigate(); }}
          >
            模型配置
          </Button>
          <ColorSchemeSwitcher />
        </Group>
        <Group gap="xs" px={8} py={4}>
          <IconMessageCircle size={14} />
          <Text c={connectionId === null ? "orange" : "dimmed"} fz={11}>
            {connectionId === null ? "正在连接…" : "已连接"}
          </Text>
        </Group>
      </AppShell.Section>
    </Stack>
  );
}

function SessionRow({
  session,
  active,
  connectionId,
  onOpen,
  onArchivedCurrent,
}: {
  session: SessionSummary;
  active: boolean;
  connectionId: string | null;
  onOpen: () => void;
  onArchivedCurrent: () => void;
}) {
  const dispatch = useAppDispatch();
  const activity = useAppSelector(
    (state) => state.runtime.sessions[session.session_id]?.activity ?? session.activity,
  );
  const showSpinner = activity === "running" || session.has_active_subagents;
  const activityLabel = activity === "running"
    ? "运行中"
    : session.has_active_subagents
      ? "等待子 Agent"
      : "空闲";
  const unread = useAppSelector(
    (state) => state.runtime.sessions[session.session_id]?.unread ?? 0,
  );
  const [archiveSession] = useArchiveSessionMutation();
  const [setSessionTitle] = useSetSessionTitleMutation();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(session.title);
  const updatedAt = formatRelativeTime(session.updated_at);

  useEffect(() => {
    if (!editing) {
      setDraft(session.title);
    }
  }, [editing, session.title]);

  async function saveTitle() {
    const title = draft.trim();
    if (connectionId === null || title === "" || title === session.title) {
      setDraft(session.title);
      setEditing(false);
      return;
    }
    await setSessionTitle({
      connectionId,
      sessionId: session.session_id,
      title,
    }).unwrap();
    setEditing(false);
  }

  async function archive() {
    if (connectionId === null) {
      return;
    }
    await archiveSession({
      connectionId,
      sessionId: session.session_id,
    }).unwrap();
    dispatch(lockDraft(session.session_id));
    if (active) {
      onArchivedCurrent();
    }
  }

  return (
    <Group
      className={
        active
          ? "session-row is-active"
          : editing
            ? "session-row is-editing"
            : "session-row"
      }
      gap={6}
      wrap="nowrap"
    >
      <Tooltip label={activityLabel} openDelay={400} position="right">
        <span
          className={`activity-dot activity-dot-${showSpinner ? "running" : "idle"}`}
          aria-label={activityLabel}
        />
      </Tooltip>
      {editing ? (
        <TextInput
          autoFocus
          aria-label="会话标题"
          className="session-title-input"
          onBlur={() => {
            void saveTitle();
          }}
          onChange={(event) => setDraft(event.currentTarget.value)}
          onClick={(event) => event.stopPropagation()}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              void saveTitle();
            }
            if (event.key === "Escape") {
              event.preventDefault();
              setDraft(session.title);
              setEditing(false);
            }
          }}
          size="xs"
          value={draft}
        />
      ) : (
        <UnstyledButton
          aria-current={active ? "page" : undefined}
          aria-label={session.title}
          className="session-row-main"
          onClick={onOpen}
          onDoubleClick={(event) => {
            event.preventDefault();
            if (connectionId !== null) {
              setEditing(true);
            }
          }}
        >
          <Tooltip label={session.title} openDelay={700} position="right">
            <Text className="session-title" fz={13} truncate>
              {session.title}
            </Text>
          </Tooltip>
        </UnstyledButton>
      )}
      {editing ? null : (
        <Group className="session-row-meta" gap={2} wrap="nowrap">
          {unread > 0 ? (
            <Badge size="xs" variant="filled">
              {unread}
            </Badge>
          ) : updatedAt === "" ? null : (
            <Text className="session-time" c="dimmed" fz={11}>
              {updatedAt}
            </Text>
          )}
          <Group className="session-actions" gap={0} wrap="nowrap">
            <Tooltip label="重命名" openDelay={400} position="right">
              <ActionIcon
                aria-label={`重命名 ${session.title}`}
                disabled={connectionId === null}
                onClick={(event) => {
                  event.stopPropagation();
                  setEditing(true);
                }}
                size="sm"
                variant="subtle"
              >
                <IconPencil size={13} />
              </ActionIcon>
            </Tooltip>
            <Tooltip label="归档" openDelay={400} position="right">
              <ActionIcon
                aria-label={`归档 ${session.title}`}
                disabled={connectionId === null}
                onClick={(event) => {
                  event.stopPropagation();
                  void archive();
                }}
                size="sm"
                variant="subtle"
              >
                <IconArchive size={13} />
              </ActionIcon>
            </Tooltip>
          </Group>
        </Group>
      )}
    </Group>
  );
}
