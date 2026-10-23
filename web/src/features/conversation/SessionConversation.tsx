import { ActionIcon, Alert, Button, Drawer, Group, Text } from "@mantine/core";
import { useDidUpdate, useMediaQuery } from "@mantine/hooks";
import { IconArrowLeft, IconFolder, IconLayoutSidebarRightCollapse, IconLayoutSidebarRightExpand, IconUsersGroup } from "@tabler/icons-react";
import { useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { useGetAgentsQuery, useGetConversationQuery, useGetGroupsQuery, useGetWorkspacesQuery, useUnlinkProjectMutation } from "../../api/redpandaApi";
import { useAppSelector } from "../../app/hooks";
import { AgentStatus } from "../sessions/AgentStatus";
import { LinkProjectModal } from "../sessions/LinkProjectModal";
import { EditableAgentName } from "../sessions/EditableAgentName";
import { EditableAgentDirection } from "../sessions/EditableAgentDirection";

import { Conversation } from "./Conversation";
import { RoutineList } from "./RoutineList";
import { BotAvatar } from "./ChatPresentation";

export function SessionConversation() {
  const { sessionId } = useParams();
  const fromGroup = (useLocation().state as { fromGroup?: { id: string; name: string } } | null)?.fromGroup;
  const { data: agents = [] } = useGetAgentsQuery();
  const { currentData: history } = useGetConversationQuery(sessionId ?? "", { skip: sessionId === undefined });
  const { data: workspaces = [] } = useGetWorkspacesQuery();
  const { data: groups = [] } = useGetGroupsQuery();
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const [detailsOpened, setDetailsOpened] = useState(false);
  const narrow = useMediaQuery("(max-width: 73.75em)");
  useDidUpdate(() => setDetailsOpened(false), [narrow]);
  const agent = agents.find((item) => item.session_id === sessionId);
  const name = agent?.name ?? history?.agent_name ?? "Agent";
  const [linking, setLinking] = useState(false);
  const [unlinkProject, unlinking] = useUnlinkProjectMutation();
  const [unlinkError, setUnlinkError] = useState<string | null>(null);
  const agentGroups = groups.filter((group) => group.members.includes(agent?.name ?? ""));
  const details = agent === undefined ? null : (
    <>
      <div className="agent-details-profile">
        <BotAvatar name={agent.name} className="agent-details-avatar" />
        <EditableAgentName key={agent.session_id} agent={agent} connectionId={connectionId} size="md" />
        <AgentStatus agent={agent} />
      </div>
      <EditableAgentDirection key={`direction-${agent.session_id}`} agent={agent} connectionId={connectionId} />
      <div className="agent-details-section">
        <Text className="agent-details-label" size="xs" c="dimmed">代码项目</Text>
        {agent.projects.map((project) => <div key={project.workspace_id}>
          <Group gap={6} wrap="nowrap" justify="space-between">
            <div className="agent-details-project"><IconFolder size={16} /><span>{project.name}</span></div>
            <Button size="compact-xs" variant="subtle" color="gray" aria-label={`解除 ${project.name}`}
              disabled={connectionId === null || unlinking.isLoading}
              onClick={() => {
                if (connectionId === null) return;
                setUnlinkError(null);
                unlinkProject({ connectionId, sessionId: agent.session_id, name: project.name }).unwrap()
                  .catch((cause) => setUnlinkError(typeof cause === "string" ? cause : "解除项目失败"));
              }}>解除</Button>
          </Group>
          <Text className="agent-details-path" size="xs" c="dimmed">{project.directory}</Text>
        </div>)}
        {unlinkError === null ? null : <Alert color="orange" p="xs">{unlinkError}</Alert>}
        <Button size="compact-xs" variant="light" disabled={connectionId === null} onClick={() => setLinking(true)}>关联项目</Button>
        <LinkProjectModal opened={linking} agent={agent} workspaces={workspaces} connectionId={connectionId}
          onClose={() => setLinking(false)} />
      </div>
      {agentGroups.length === 0 ? null : <div className="agent-details-section">
        <Text className="agent-details-label" size="xs" c="dimmed">参与的群聊</Text>
        {agentGroups.map((group) => <Link className="agent-details-project" key={group.group_id} to={`/groups/${encodeURIComponent(group.group_id)}`}>
          <IconUsersGroup size={16} /><span>{group.name}</span>
        </Link>)}
      </div>}
      <RoutineList key={agent.session_id} sessionId={agent.session_id} connectionId={connectionId}
        onOpenTimeline={narrow ? () => setDetailsOpened(false) : undefined} />
    </>
  );
  return (
    <div className="session-page">
      <div className="session-main">
      <header className="page-header session-header">
        {fromGroup === undefined ? null : <ActionIcon component={Link} to={`/groups/${encodeURIComponent(fromGroup.id)}`} aria-label={`返回 ${fromGroup.name}`} title={`返回 ${fromGroup.name}`} variant="subtle" color="gray"><IconArrowLeft size={18} /></ActionIcon>}
        <Group gap={10} wrap="nowrap" className="session-header-identity">
          <BotAvatar name={name} />
          <div className="session-header-name">{agent === undefined ? <Text fw={600} size="sm">{name}</Text>
            : <EditableAgentName key={agent.session_id} agent={agent} connectionId={connectionId} size="sm" />}
            <div className="session-header-subtitle">{agent === undefined ? null : <AgentStatus agent={agent} />}<span>{agent?.direction == null ? "一起交流想法、处理任务" : `· ${agent.direction}`}</span></div></div>
        </Group>
        {agent === undefined || agent.projects.length === 0 ? null : <span className="session-project-link">
          <IconFolder size={15} /><span>{agent.projects.map((project) => project.name).join("、")}</span></span>}
        <ActionIcon aria-label={detailsOpened ? "收起 Bot 资料" : "显示 Bot 资料"} aria-expanded={detailsOpened} variant="subtle" color="gray" onClick={() => setDetailsOpened((opened) => !opened)}>{detailsOpened ? <IconLayoutSidebarRightCollapse size={18} /> : <IconLayoutSidebarRightExpand size={18} />}</ActionIcon>
      </header>
      <div className="session-page-body"><Conversation key={sessionId} agentName={agent?.name ?? history?.agent_name ?? undefined} agentNames={agents.map((item) => item.name)} /></div>
      </div>
      {agent === undefined ? null : narrow ? (
        <Drawer opened={detailsOpened} onClose={() => setDetailsOpened(false)}
          title="Agent 资料" position="right" size="min(292px, 85vw)"
          classNames={{ header: "details-heading", body: "details-body" }}
          closeButtonProps={{ "aria-label": "关闭 Agent 资料" }}>
          {details}
        </Drawer>
      ) : !detailsOpened ? null : (
        <aside className="agent-details" aria-label="Agent 资料"><header className="details-heading"><Text fw={600} fz={12}>Bot 资料</Text><ActionIcon aria-label="关闭 Bot 资料" variant="subtle" color="gray" onClick={() => setDetailsOpened(false)}><IconLayoutSidebarRightCollapse size={17} /></ActionIcon></header>{details}</aside>
      )}
    </div>
  );
}
