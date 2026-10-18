import {
  ActionIcon,
  AppShell,
  Box,
  Button,
  Collapse,
  Group,
  ScrollArea,
  Skeleton,
  Stack,
  Text,
  Tooltip,
  UnstyledButton,
} from "@mantine/core";
import {
  IconFolderPlus,
  IconMessageCircle,
  IconPlus,
  IconAdjustments,
  IconAlertCircle,
} from "@tabler/icons-react";
import { useDisclosure } from "@mantine/hooks";
import { useState } from "react";
import { useMatch, useNavigate } from "react-router-dom";

import type { AgentRosterEntry, Workspace } from "../../api/contracts";
import {
  useGetAgentsQuery,
  useGetWorkspacesQuery,
} from "../../api/redpandaApi";
import { useAppSelector } from "../../app/hooks";
import { redPandaMark } from "../../app/brand";
import { ColorSchemeSwitcher } from "../../app/ColorSchemeSwitcher";
import { CreateAgentModal } from "./CreateAgentModal";
import { CreateWorkspaceModal } from "./CreateWorkspaceModal";
import { RemoveWorkspaceModal } from "./RemoveWorkspaceModal";
import { RenameWorkspaceModal } from "./RenameWorkspaceModal";
import { WorkspaceHeader } from "./WorkspaceHeader";
import {
  readCollapsedWorkspaces,
  writeCollapsedWorkspaces,
} from "./workspaces";

type SessionSidebarProps = {
  onNavigate: () => void;
};

const STATUS_LABEL = {
  working: "干活",
  authorization: "等授权",
  waiting: "等人回话",
  idle: "空闲",
} as const;

export function SessionSidebar({ onNavigate }: SessionSidebarProps) {
  const navigate = useNavigate();
  const sessionId = useMatch("/sessions/:sessionId")?.params.sessionId;
  const routeWorkspaceId = useMatch("/workspaces/:workspaceId")?.params
    .workspaceId;
  const modelSettingsActive = useMatch("/settings/models") !== null;
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const { data: agents = [], isLoading } = useGetAgentsQuery();
  const { data: workspaces = [], isLoading: workspacesLoading } =
    useGetWorkspacesQuery();
  const [
    createWorkspaceOpened,
    { open: openCreateWorkspace, close: closeCreateWorkspace },
  ] = useDisclosure(false);
  const [creatingWorkspaceId, setCreatingWorkspaceId] = useState<string | null>(
    null,
  );
  const [collapsedWorkspaces, setCollapsedWorkspaces] = useState(
    readCollapsedWorkspaces,
  );
  const [removingWorkspace, setRemovingWorkspace] = useState<Workspace | null>(
    null,
  );
  const [renamingWorkspace, setRenamingWorkspace] = useState<Workspace | null>(null);
  const workspaceRecords = new Map(
    workspaces.map((workspace) => [workspace.workspace_id, workspace]),
  );
  const groups = workspaces.map((workspace) => ({
    id: workspace.workspace_id,
    name: workspace.name,
    agents: agents.filter((agent) => agent.workspace_id === workspace.workspace_id),
  }));
  const currentWorkspaceId = agents.find(
    (agent) => agent.session_id === sessionId,
  )?.workspace_id ?? routeWorkspaceId ?? null;
  const canCreateAgent = connectionId !== null && workspaces.length > 0;

  function toggleWorkspace(workspaceId: string) {
    setCollapsedWorkspaces((current) => {
      const next = { ...current, [workspaceId]: !current[workspaceId] };
      writeCollapsedWorkspaces(next);
      return next;
    });
  }

  function openCreate(workspaceId: string) {
    setCollapsedWorkspaces((current) => {
      if (current[workspaceId] !== true) {
        return current;
      }
      const next = { ...current, [workspaceId]: false };
      writeCollapsedWorkspaces(next);
      return next;
    });
    setCreatingWorkspaceId(workspaceId);
  }

  function leaveRemovedWorkspace(workspace: Workspace) {
    if (
      currentWorkspaceId === workspace.workspace_id ||
      routeWorkspaceId === workspace.workspace_id
    ) {
      const next = groups.find((group) => group.id !== workspace.workspace_id);
      navigate(
        next === undefined ? "/" : `/workspaces/${encodeURIComponent(next.id)}`,
      );
    }
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
          disabled={!canCreateAgent}
          onClick={() => {
            const target = currentWorkspaceId ?? workspaces[0]?.workspace_id;
            if (target !== undefined) {
              openCreate(target);
            }
          }}
          radius="md"
          variant="light"
        >
          新建 Agent
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
                  添加一个项目，再在里面新建 Agent。
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
            <CreateAgentModal
              connectionId={connectionId}
              onClose={() => setCreatingWorkspaceId(null)}
              onCreated={(createdId) => {
                setCreatingWorkspaceId(null);
                navigate(`/sessions/${encodeURIComponent(createdId)}`);
                onNavigate();
              }}
              opened={creatingWorkspaceId !== null}
              workspaceId={creatingWorkspaceId}
              workspaces={workspaces}
            />
            {removingWorkspace === null ? null : (
              <RemoveWorkspaceModal
                onClose={() => setRemovingWorkspace(null)}
                onRemoved={() => leaveRemovedWorkspace(removingWorkspace)}
                opened
                workspace={removingWorkspace}
              />
            )}
            {renamingWorkspace === null ? null : (
              <RenameWorkspaceModal
                onClose={() => setRenamingWorkspace(null)}
                workspace={renamingWorkspace}
              />
            )}
            {groups.map((workspace) => {
              const collapsed = collapsedWorkspaces[workspace.id] === true;
              return (
                <Box
                  className={
                    workspace.id === currentWorkspaceId
                      ? "workspace-group is-current"
                      : "workspace-group"
                  }
                  key={workspace.id}
                >
                  <WorkspaceHeader
                    name={workspace.name}
                    collapsed={collapsed}
                    connectionId={connectionId}
                    registered={workspaceRecords.has(workspace.id)}
                    onToggle={() => toggleWorkspace(workspace.id)}
                    onCreate={() => openCreate(workspace.id)}
                    onRename={() => setRenamingWorkspace(workspaceRecords.get(workspace.id)!)}
                    onRemove={() => setRemovingWorkspace(workspaceRecords.get(workspace.id)!)}
                  />
                  <Collapse expanded={!collapsed}>
                    <Stack className="workspace-sessions" gap={3}>
                      {workspace.agents.length === 0 ? (
                        <Text c="dimmed" fz={12} px={8} py={4}>
                          还没有 Agent
                        </Text>
                      ) : workspace.agents.map((agent) => (
                        <AgentRow
                          key={agent.session_id}
                          active={agent.session_id === sessionId}
                          agent={agent}
                          onOpen={() => {
                            navigate(
                              `/sessions/${encodeURIComponent(agent.session_id)}`,
                            );
                            onNavigate();
                          }}
                        />
                      ))}
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

function AgentRow({
  agent,
  active,
  onOpen,
}: {
  agent: AgentRosterEntry;
  active: boolean;
  onOpen: () => void;
}) {
  const live = useAppSelector(
    (state) => state.runtime.sessions[agent.session_id]?.activity,
  );
  const status = agent.status === "authorization" ? "authorization"
    : live === "running" ? "working"
    : agent.status;
  const label = STATUS_LABEL[status];
  const dot = status === "authorization" ? "attention"
    : status === "working" ? "running" : "idle";

  return (
    <Group
      className={active ? "session-row is-active" : "session-row"}
      gap={6}
      wrap="nowrap"
    >
      <Tooltip label={label} openDelay={400} position="right">
        <span
          className={`activity-dot activity-dot-${dot}`}
          aria-label={label}
        >
          {status === "authorization" ? <IconAlertCircle size={13} /> : null}
        </span>
      </Tooltip>
      <UnstyledButton
        aria-current={active ? "page" : undefined}
        aria-label={agent.name}
        className="session-row-main"
        onClick={onOpen}
      >
        <Tooltip label={agent.direction ?? agent.name} openDelay={700} position="right">
          <Stack gap={0}>
            <Text className="session-title" fz={13} truncate>
              {agent.name}
            </Text>
            {agent.direction === null ? null : (
              <Text c="dimmed" fz={11} truncate>
                {agent.direction}
              </Text>
            )}
          </Stack>
        </Tooltip>
      </UnstyledButton>
    </Group>
  );
}
