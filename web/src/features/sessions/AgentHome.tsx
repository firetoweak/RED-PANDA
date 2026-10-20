import { Alert, Button, Group, Skeleton, Stack, Text, Title } from "@mantine/core";
import { IconArrowRight, IconFolderPlus, IconPlus, IconUsersGroup } from "@tabler/icons-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useGetAgentsQuery, useGetGroupsQuery, useGetWorkspacesQuery } from "../../api/redpandaApi";
import { useAppSelector } from "../../app/hooks";
import { AgentStatus } from "./AgentStatus";
import { CreateAgentModal } from "./CreateAgentModal";
import { CreateWorkspaceModal } from "./CreateWorkspaceModal";
import { CreateGroupModal } from "../groups/CreateGroupModal";

export function AgentHome() {
  const navigate = useNavigate();
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const { data: workspaces = [] } = useGetWorkspacesQuery();
  const { data: agents = [], isLoading, isError } = useGetAgentsQuery();
  const { data: groups = [] } = useGetGroupsQuery();
  const [creating, setCreating] = useState(false);
  const [projectCreating, setProjectCreating] = useState(false);
  const [groupCreating, setGroupCreating] = useState(false);
  return <section className="agent-home">
    <header className="page-header"><Text fw={500} size="sm">工作台</Text><Group gap="xs">
      <Button variant="subtle" color="gray" size="compact-sm" leftSection={<IconFolderPlus size={15} />} onClick={() => setProjectCreating(true)}>添加代码项目</Button>
      <Button size="compact-sm" leftSection={<IconPlus size={15} />} disabled={connectionId === null} onClick={() => setCreating(true)}>新建 Bot</Button>
    </Group></header>
    <div className="home-scroll"><div className="home-content">
      <div className="home-intro"><Title order={1}>与你的 Bot，一起把事情做好。</Title><Text c="dimmed" className="home-description">直接开始对话与协作。每位 Bot 都有自己的工作目录，代码项目按需关联。</Text></div>
      {isError ? <Alert color="red" title="工作台加载失败">请检查后端连接后刷新。</Alert> : null}
      {isLoading ? <Stack><Skeleton h={100} /></Stack> : agents.length === 0 ? <div className="onboarding-panel"><Title order={2}>创建第一位 Bot</Title><Text c="dimmed">先聊想法、分析资料，或一起推敲方案，无需准备 Git 仓库。</Text><Button disabled={connectionId === null} onClick={() => setCreating(true)}>创建 Bot</Button></div> : null}
      <div className="agent-grid">{agents.map((agent) => <Link className="agent-card" key={agent.session_id} to={`/sessions/${encodeURIComponent(agent.session_id)}`}><div className="agent-card-top"><span className="agent-avatar">{agent.name.slice(0, 1)}</span><AgentStatus agent={agent} /></div><Text fw={600}>{agent.name}</Text><Text c="dimmed" size="sm">{agent.direction ?? "打开对话，开始协作。"}</Text><span className="agent-card-action">打开对话<IconArrowRight size={15} /></span></Link>)}</div>
      <Group justify="space-between" mt="xl"><Title order={2}>群聊</Title><Button variant="default" size="compact-sm" disabled={connectionId === null || agents.length < 2} onClick={() => setGroupCreating(true)}>新建群聊</Button></Group>
      <div className="home-groups">{groups.map((group) => <Link key={group.group_id} to={`/groups/${encodeURIComponent(group.group_id)}`}><IconUsersGroup size={16} /><span>{group.name}</span><Text size="xs" c="dimmed">{group.members.length} 位成员</Text></Link>)}</div>
    </div></div>
    <CreateWorkspaceModal opened={projectCreating} onClose={() => setProjectCreating(false)} />
    <CreateAgentModal opened={creating} workspaceId={null} workspaces={workspaces} connectionId={connectionId} onClose={() => setCreating(false)} onCreated={(id) => { setCreating(false); navigate(`/sessions/${encodeURIComponent(id)}`); }} />
    <CreateGroupModal opened={groupCreating} agents={agents} connectionId={connectionId} onClose={() => setGroupCreating(false)} onCreated={(id) => { setGroupCreating(false); navigate(`/groups/${encodeURIComponent(id)}`); }} />
  </section>;
}
