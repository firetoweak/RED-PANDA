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
  TextInput,
  Tooltip,
} from "@mantine/core";
import {
  IconFolderPlus,
  IconLayoutDashboard,
  IconInbox,
  IconSearch,
  IconX,
  IconAdjustments,
} from "@tabler/icons-react";
import { useDisclosure } from "@mantine/hooks";
import { useState } from "react";
import { useMatch, useNavigate } from "react-router-dom";

import type { Workspace } from "../../api/contracts";
import {
  useGetAgentsQuery,
  useGetGroupsQuery,
  useGetInboxQuery,
  useGetWorkspacesQuery,
} from "../../api/redpandaApi";
import { useAppSelector } from "../../app/hooks";
import { redPandaMark } from "../../app/brand";
import { ColorSchemeSwitcher } from "../../app/ColorSchemeSwitcher";
import { CreateGroupModal } from "../groups/CreateGroupModal";
import { CreateAgentModal } from "./CreateAgentModal";
import { CreateWorkspaceModal } from "./CreateWorkspaceModal";
import { RemoveWorkspaceModal } from "./RemoveWorkspaceModal";
import { RenameWorkspaceModal } from "./RenameWorkspaceModal";
import { WorkspaceHeader } from "./WorkspaceHeader";
import { WorkspaceRoster } from "./WorkspaceRoster";
import {
  readCollapsedWorkspaces,
  writeCollapsedWorkspaces,
} from "./workspaces";

type AgentSidebarProps = {
  onNavigate: () => void;
};

export function AgentSidebar({ onNavigate }: AgentSidebarProps) {
  const navigate = useNavigate();
  const sessionId = useMatch("/sessions/:sessionId")?.params.sessionId;
  const groupId = useMatch("/groups/:groupId")?.params.groupId;
  const routeWorkspaceId = useMatch("/workspaces/:workspaceId")?.params
    .workspaceId;
  const modelSettingsActive = useMatch("/settings/models") !== null;
  const inboxActive = useMatch("/inbox") !== null;
  const homeActive = useMatch("/") !== null || routeWorkspaceId !== undefined;
  const [search, setSearch] = useState("");
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const { data: agents = [], isLoading } = useGetAgentsQuery();
  const { data: inbox = [] } = useGetInboxQuery();
  const { data: chatGroups = [] } = useGetGroupsQuery();
  const [groupWorkspaceId, setGroupWorkspaceId] = useState<string | null>(null);
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
    chats: chatGroups.filter((chat) => chat.workspace_id === workspace.workspace_id),
  }));
  const query = search.trim().toLocaleLowerCase();
  const filteredGroups = groups.map((workspace) => {
    if (workspace.name.toLocaleLowerCase().includes(query)) return workspace;
    return { ...workspace,
      agents: workspace.agents.filter((agent) => `${agent.name} ${agent.direction ?? ""}`.toLocaleLowerCase().includes(query)),
      chats: workspace.chats.filter((chat) => chat.name.toLocaleLowerCase().includes(query)),
    };
  }).filter((workspace) => query === "" || workspace.name.toLocaleLowerCase().includes(query)
    || workspace.agents.length > 0 || workspace.chats.length > 0);
  const currentWorkspaceId = agents.find(
    (agent) => agent.session_id === sessionId,
  )?.workspace_id ?? chatGroups.find((chat) => chat.group_id === groupId)?.workspace_id
    ?? routeWorkspaceId ?? null;

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
    <Stack h="100%" gap="md" className="sidebar-content">
      <AppShell.Section>
        <Group className="sidebar-brand" gap="sm" px={6} py={4} wrap="nowrap">
          <img className="brand-mark" src={redPandaMark} alt="" width={32} height={32} />
          <Box>
            <Text className="brand-name" fw={700} lh={1.15} size="sm">
              RED PANDA
            </Text>
            <Text c="dimmed" fz={11}>
              你的编码协作伙伴
            </Text>
          </Box>
        </Group>
      </AppShell.Section>

      <AppShell.Section>
        <Button fullWidth className={homeActive ? "sidebar-nav is-active" : "sidebar-nav"}
          justify="flex-start" leftSection={<IconLayoutDashboard size={17} />}
          color="gray" variant="subtle" onClick={() => { navigate("/"); onNavigate(); }}>工作台</Button>
        <Button
          className={inboxActive ? "sidebar-nav is-active" : "sidebar-nav"}
          aria-label={inbox.length === 0 ? "待处理" : `待处理 · ${inbox.length}`}
          fullWidth
          justify="flex-start"
          leftSection={<IconInbox size={17} />}
          rightSection={inbox.length === 0 ? undefined : <span className="sidebar-count" aria-hidden="true">{inbox.length}</span>}
          mt={3}
          onClick={() => { navigate("/inbox"); onNavigate(); }}
          radius="md"
          color="gray"
          variant="subtle"
        >
          待处理
        </Button>
        <TextInput aria-label="搜索 Agent、群聊或工作区" className="sidebar-search" mt="md" size="xs"
          leftSection={<IconSearch size={14} />} placeholder="搜索 Agent 或项目" value={search}
          onChange={(event) => setSearch(event.currentTarget.value)}
          rightSection={search === "" ? undefined : <ActionIcon aria-label="清除搜索" size="xs" color="gray" variant="subtle" onClick={() => setSearch("")}><IconX size={12} /></ActionIcon>} />
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
            <CreateGroupModal
              agents={agents}
              connectionId={connectionId}
              onClose={() => setGroupWorkspaceId(null)}
              onCreated={(createdId) => {
                setGroupWorkspaceId(null);
                navigate(`/groups/${encodeURIComponent(createdId)}`);
                onNavigate();
              }}
              opened={groupWorkspaceId !== null}
              workspaceId={groupWorkspaceId}
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
            {query !== "" && filteredGroups.length === 0 ? <Text c="dimmed" size="xs" p="sm">没有找到匹配的 Agent、群聊或工作区</Text> : null}
            {filteredGroups.map((workspace) => {
              const collapsed = query === "" && collapsedWorkspaces[workspace.id] === true;
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
                    onCreateGroup={() => setGroupWorkspaceId(workspace.id)}
                    onRename={() => setRenamingWorkspace(workspaceRecords.get(workspace.id)!)}
                    onRemove={() => setRemovingWorkspace(workspaceRecords.get(workspace.id)!)}
                  />
                  <Collapse expanded={!collapsed}>
                    <WorkspaceRoster workspaceId={workspace.id} workspaceName={workspace.name}
                      agents={workspace.agents} chats={workspace.chats} sessionId={sessionId} groupId={groupId}
                      searching={query !== ""} onOpen={(path) => { navigate(path); onNavigate(); }} />
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
          <span className={connectionId === null ? "connection-indicator" : "connection-indicator is-connected"} />
          <Text c={connectionId === null ? "orange" : "dimmed"} fz={11}>
            {connectionId === null ? "正在连接…" : "已连接"}
          </Text>
        </Group>
      </AppShell.Section>
    </Stack>
  );
}
