import { Alert, Button, Group, Skeleton, Stack, Text, Title } from "@mantine/core";
import { IconArrowRight, IconPlus, IconUsersGroup } from "@tabler/icons-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useGetAgentsQuery, useGetGroupsQuery, useGetWorkspacesQuery } from "../../api/redpandaApi";
import { useAppSelector } from "../../app/hooks";
import { AgentStatus } from "./AgentStatus";
import { CreateAgentModal } from "./CreateAgentModal";
import { CreateGroupModal } from "../groups/CreateGroupModal";
import { BotAvatar } from "../conversation/ChatPresentation";

export function AgentHome() {
  const navigate = useNavigate();
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const { data: workspaces = [] } = useGetWorkspacesQuery();
  const { data: agents = [], isLoading, isError } = useGetAgentsQuery();
  const { data: groups = [] } = useGetGroupsQuery();
  const [creating, setCreating] = useState(false);
  const [groupCreating, setGroupCreating] = useState(false);
  return <section className="agent-home">
    <header className="page-header"><Text fw={600} size="md">对话</Text><Group gap="xs">
      <Button size="compact-sm" leftSection={<IconPlus size={15} />} disabled={connectionId === null} onClick={() => setCreating(true)}>新建 Bot</Button>
    </Group></header>
    <div className="home-scroll"><div className="home-content">
      <div className="home-intro"><Title order={1}>找一位 Bot，聊聊接下来要做的事。</Title><Text c="dimmed" className="home-description">从私聊开始，需要一起讨论时再邀请伙伴加入群聊。代码项目按需关联。</Text></div>
      {isError ? <Alert color="red" title="工作台加载失败">请检查后端连接后刷新。</Alert> : null}
      {isLoading ? <Stack><Skeleton h={100} /></Stack> : agents.length === 0 ? <div className="onboarding-panel"><Title order={2}>创建第一位 Bot</Title><Text c="dimmed">先聊想法、分析资料，或一起推敲方案，无需准备 Git 仓库。</Text><Button disabled={connectionId === null} onClick={() => setCreating(true)}>创建 Bot</Button></div> : null}
      <div className="agent-grid">{agents.map((agent) => <Link className="agent-card" key={agent.session_id} to={`/sessions/${encodeURIComponent(agent.session_id)}`}><BotAvatar name={agent.name} sessionId={agent.session_id} /><div className="home-agent-identity"><Text fw={600}>{agent.name}</Text><Text c="dimmed" size="sm">{agent.direction ?? "打开对话，开始协作。"}</Text></div><AgentStatus agent={agent} /><span className="agent-card-action">打开对话<IconArrowRight size={15} /></span></Link>)}</div>
      <Group justify="space-between" mt="xl"><Title order={2}>群聊</Title><Button variant="default" size="compact-sm" disabled={connectionId === null || agents.length < 2} onClick={() => setGroupCreating(true)}>新建群聊</Button></Group>
      <div className="home-groups">{groups.map((group) => <Link key={group.group_id} to={`/groups/${encodeURIComponent(group.group_id)}`}><IconUsersGroup size={16} /><span>{group.name}</span><Text size="xs" c="dimmed">{group.members.length} 位成员</Text></Link>)}</div>
    </div></div>
    <CreateAgentModal opened={creating} workspaces={workspaces} connectionId={connectionId} onClose={() => setCreating(false)} onCreated={(id) => { setCreating(false); navigate(`/sessions/${encodeURIComponent(id)}`); }} />
    <CreateGroupModal opened={groupCreating} agents={agents} connectionId={connectionId} onClose={() => setGroupCreating(false)} onCreated={(id) => { setGroupCreating(false); navigate(`/groups/${encodeURIComponent(id)}`); }} />
  </section>;
}
