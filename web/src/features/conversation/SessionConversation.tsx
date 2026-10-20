import { Group, Text, UnstyledButton } from "@mantine/core";
import { IconFolder, IconUsersGroup } from "@tabler/icons-react";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useGetAgentsQuery, useGetGroupsQuery, useGetWorkspacesQuery } from "../../api/redpandaApi";
import { AgentStatus } from "../sessions/AgentStatus";

import { Conversation } from "./Conversation";

export function SessionConversation() {
  const { sessionId } = useParams();
  const { data: agents = [] } = useGetAgentsQuery();
  const { data: workspaces = [] } = useGetWorkspacesQuery();
  const { data: groups = [] } = useGetGroupsQuery();
  const [detailsOpened, setDetailsOpened] = useState(false);
  const agent = agents.find((item) => item.session_id === sessionId);
  const workspace = workspaces.find((item) => item.workspace_id === agent?.workspace_id);
  const agentGroups = groups.filter((group) => group.members.includes(agent?.name ?? ""));
  return (
    <div className="session-page">
      <div className="session-main">
      <header className="page-header session-header">
        <Group component={UnstyledButton} gap={10} wrap="nowrap" className="session-header-identity"
          aria-label={agent?.name ?? "Agent"} aria-expanded={detailsOpened}
          onClick={() => setDetailsOpened((opened) => !opened)}>
          <span className="agent-avatar agent-avatar-small">{agent?.name.slice(0, 1) ?? "A"}</span>
          <div className="session-header-name"><Text fw={600} size="sm" truncate>{agent?.name ?? "Agent"}</Text>
            <Text c="dimmed" fz={11} truncate>{agent?.direction ?? "一起交流想法、处理任务"}</Text></div>
          {agent === undefined ? null : <AgentStatus agent={agent} />}
        </Group>
        {workspace === undefined ? null : <span className="session-project-link">
          <IconFolder size={15} /><span>{workspace.name}</span></span>}
      </header>
      <div className="session-page-body"><Conversation key={sessionId} agentName={agent?.name} /></div>
      </div>
      {!detailsOpened || agent === undefined ? null : (
        <aside className="agent-details" aria-label="Agent 资料">
          <div className="agent-details-profile">
            <span className="agent-avatar agent-details-avatar" aria-hidden="true">{agent.name.slice(0, 1)}</span>
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
        </aside>
      )}
    </div>
  );
}
