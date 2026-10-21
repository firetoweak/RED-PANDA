import { ActionIcon, Drawer, Group, Text, UnstyledButton } from "@mantine/core";
import { useMediaQuery } from "@mantine/hooks";
import { IconArrowLeft, IconFolder, IconLayoutSidebarRightCollapse, IconLayoutSidebarRightExpand, IconUsersGroup } from "@tabler/icons-react";
import { useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { useGetAgentsQuery, useGetGroupsQuery, useGetWorkspacesQuery } from "../../api/redpandaApi";
import { useAppSelector } from "../../app/hooks";
import { AgentStatus } from "../sessions/AgentStatus";

import { Conversation } from "./Conversation";
import { RoutineList } from "./RoutineList";
import { BotAvatar } from "./ChatPresentation";

export function SessionConversation() {
  const { sessionId } = useParams();
  const fromGroup = (useLocation().state as { fromGroup?: { id: string; name: string } } | null)?.fromGroup;
  const { data: agents = [] } = useGetAgentsQuery();
  const { data: workspaces = [] } = useGetWorkspacesQuery();
  const { data: groups = [] } = useGetGroupsQuery();
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const [detailsOpened, setDetailsOpened] = useState(false);
  const narrow = useMediaQuery("(max-width: 64em)");
  const agent = agents.find((item) => item.session_id === sessionId);
  const workspace = workspaces.find((item) => item.workspace_id === agent?.workspace_id);
  const agentGroups = groups.filter((group) => group.members.includes(agent?.name ?? ""));
  const details = agent === undefined ? null : (
    <>
      <div className="agent-details-profile">
        <BotAvatar name={agent.name} className="agent-details-avatar" />
        <Text fw={600} size="md">{agent.name}</Text>
        <AgentStatus agent={agent} />
      </div>
      <div className="agent-details-section">
        <Text className="agent-details-label" size="xs" c="dimmed">负责方向</Text>
        <Text size="sm">{agent.direction ?? "尚未设置负责方向"}</Text>
      </div>
      {workspace === undefined ? null : <div className="agent-details-section">
        <Text className="agent-details-label" size="xs" c="dimmed">所在项目</Text>
        <div className="agent-details-project">
          <IconFolder size={16} /><span>{workspace.name}</span>
        </div>
        <Text className="agent-details-path" size="xs" c="dimmed">{workspace.task_root}</Text>
      </div>}
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
        <Group component={UnstyledButton} gap={10} wrap="nowrap" className="session-header-identity"
          aria-label={agent?.name ?? "Agent"} aria-expanded={detailsOpened}
          onClick={() => setDetailsOpened((opened) => !opened)}>
          <BotAvatar name={agent?.name ?? "Agent"} />
          <div className="session-header-name"><Text fw={600} size="sm" truncate>{agent?.name ?? "Agent"}</Text>
            <div className="session-header-subtitle">{agent === undefined ? null : <AgentStatus agent={agent} />}<span>{agent?.direction == null ? "一起交流想法、处理任务" : `· ${agent.direction}`}</span></div></div>
        </Group>
        {workspace === undefined ? null : <span className="session-project-link">
          <IconFolder size={15} /><span>{workspace.name}</span></span>}
        <ActionIcon aria-label={detailsOpened ? "收起 Bot 资料" : "显示 Bot 资料"} aria-expanded={detailsOpened} variant="subtle" color="gray" onClick={() => setDetailsOpened((opened) => !opened)}>{detailsOpened ? <IconLayoutSidebarRightCollapse size={18} /> : <IconLayoutSidebarRightExpand size={18} />}</ActionIcon>
      </header>
      <div className="session-page-body"><Conversation key={sessionId} agentName={agent?.name} agentDirection={agent?.direction} agentNames={agents.map((item) => item.name)} /></div>
      </div>
      {agent === undefined ? null : narrow ? (
        <Drawer opened={detailsOpened} onClose={() => setDetailsOpened(false)}
          title="Agent 资料" position="right" size={320}
          closeButtonProps={{ "aria-label": "关闭 Agent 资料" }}>
          {details}
        </Drawer>
      ) : !detailsOpened ? null : (
        <aside className="agent-details" aria-label="Agent 资料"><header className="details-heading"><Text fw={600} fz={12}>Bot 资料</Text><ActionIcon aria-label="关闭 Bot 资料" variant="subtle" color="gray" onClick={() => setDetailsOpened(false)}><IconLayoutSidebarRightCollapse size={17} /></ActionIcon></header>{details}</aside>
      )}
    </div>
  );
}
