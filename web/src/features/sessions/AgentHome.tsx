import { Alert, Button, Group, Skeleton, Stack, Text, Title } from "@mantine/core";
import { IconArrowRight, IconFolder, IconFolderPlus, IconInbox, IconPlus, IconUsersGroup } from "@tabler/icons-react";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useGetAgentsQuery, useGetGroupsQuery, useGetInboxQuery, useGetWorkspacesQuery } from "../../api/redpandaApi";
import { useAppSelector } from "../../app/hooks";
import { redPandaMark } from "../../app/brand";
import { AgentStatus } from "./AgentStatus";
import { CreateAgentModal } from "./CreateAgentModal";
import { CreateWorkspaceModal } from "./CreateWorkspaceModal";

export function AgentHome() {
  const navigate = useNavigate();
  const { workspaceId } = useParams();
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const { data: workspaces = [], isLoading, isError } = useGetWorkspacesQuery();
  const { data: agents = [], isLoading: agentsLoading, isError: agentsError } = useGetAgentsQuery();
  const { data: groups = [] } = useGetGroupsQuery();
  const { data: inbox = [] } = useGetInboxQuery();
  const [workspaceOpened, setWorkspaceOpened] = useState(false);
  const [creatingIn, setCreatingIn] = useState<string | null>(null);
  const workspace = workspaces.find((item) => item.workspace_id === workspaceId);
  const visibleWorkspaces = workspaceId === undefined ? workspaces : workspace ? [workspace] : [];
  return (
    <section className="agent-home">
      <header className="page-header">
        <Group gap={8}><IconFolder size={17} /><Text fw={500} size="sm">{workspace?.name ?? "工作台"}</Text></Group>
        <Button leftSection={<IconFolderPlus size={15} />} variant="subtle" color="gray"
          size="compact-sm" onClick={() => setWorkspaceOpened(true)}>新建工作区</Button>
      </header>
      <div className="home-scroll"><div className="home-content">
        <div className="home-intro">
          <div className="home-eyebrow"><span className="brand-indicator" /> YOUR CODING TEAM</div>
          <Title order={1}>{workspace?.name ?? "让想法，成为代码。"}</Title>
          <Text c="dimmed" className="home-description">{workspaces.length === 0
            ? "连接你的项目，和 Agent 一起开始。" : "选择一位 Agent 继续对话，或为项目添加一位新伙伴。"}</Text>
        </div>
        {isError || agentsError ? <Alert color="red" title="工作台加载失败">请检查后端连接后刷新。</Alert> : null}
        {isLoading || agentsLoading ? <Stack gap="md"><Skeleton h={100} /><Skeleton h={160} /></Stack>
          : workspaceId !== undefined && workspace === undefined && !isError ? (
            <div className="home-empty"><Title order={2}>找不到这个工作区</Title>
              <Button component={Link} to="/" variant="default">返回工作台</Button></div>
          ) : workspaces.length === 0 && !isError ? (
            <div className="onboarding-panel">
              <div className="onboarding-icon"><IconFolderPlus size={27} stroke={1.4} /></div>
              <Title order={2}>从一个项目开始</Title>
              <Text c="dimmed" size="sm">选择一个已有提交的 Git 项目。每位 Agent 都有自己的工作分支，随时可以查看过程与改动。</Text>
              <Button leftSection={<IconPlus size={16} />} onClick={() => setWorkspaceOpened(true)}>创建工作区</Button>
              <div className="onboarding-steps"><span>01 连接项目</span><span>02 创建 Agent</span><span>03 开始协作</span></div>
            </div>
          ) : null}
        {inbox.length === 0 ? null : (
          <Link className="home-inbox" to="/inbox"><IconInbox size={19} />
            <span>{inbox.length} 项事项需要你处理<Text component="span" c="dimmed" size="xs">查看授权与需要回复的消息</Text></span>
            <IconArrowRight size={17} /></Link>
        )}
        {visibleWorkspaces.map((project) => {
          const projectAgents = agents.filter((agent) => agent.workspace_id === project.workspace_id);
          const projectGroups = groups.filter((group) => group.workspace_id === project.workspace_id);
          return (
            <section className="home-project" key={project.workspace_id}>
              <div className="home-project-heading">
                <div className="home-project-identity"><Group gap={8}>
                  <IconFolder size={18} stroke={1.6} />
                  <Text component={Link} to={`/workspaces/${encodeURIComponent(project.workspace_id)}`} fw={600}>{project.name}</Text>
                  <Text c="dimmed" size="xs">{projectAgents.length} 位 Agent</Text>
                </Group><Text c="dimmed" className="home-project-path" size="xs" title={project.task_root} truncate>{project.task_root}</Text></div>
                <Button disabled={connectionId === null} leftSection={<IconPlus size={14} />} variant="default" size="compact-sm"
                  onClick={() => setCreatingIn(project.workspace_id)}>新建 Agent</Button>
              </div>
              <div className="agent-grid">
                {projectAgents.map((agent) => (
                  <Link className="agent-card" key={agent.session_id} to={`/sessions/${encodeURIComponent(agent.session_id)}`}>
                    <div className="agent-card-top"><span className="agent-avatar">{agent.name.slice(0, 1)}</span><AgentStatus agent={agent} /></div>
                    <Text fw={600} truncate>{agent.name}</Text>
                    <Text c="dimmed" size="sm" lineClamp={2}>{agent.direction ?? "准备好与你一起处理这个项目。"}</Text>
                    <span className="agent-card-action">打开对话<IconArrowRight size={15} /></span>
                  </Link>
                ))}
                <button className="agent-card agent-card-add" disabled={connectionId === null} onClick={() => setCreatingIn(project.workspace_id)}>
                  <IconPlus size={24} stroke={1.4} /><span>{projectAgents.length === 0 ? "创建第一位 Agent" : "添加伙伴"}</span>
                  <span className="agent-card-add-hint">为项目分担一项工作</span>
                </button>
              </div>
              {projectGroups.length === 0 ? null : <div className="home-groups">{projectGroups.map((group) => (
                <Link key={group.group_id} to={`/groups/${encodeURIComponent(group.group_id)}`}><IconUsersGroup size={16} />
                  <span>{group.name}</span><Text component="span" c="dimmed" size="xs">{group.members.length} 位成员</Text><IconArrowRight size={14} /></Link>
              ))}</div>}
            </section>
          );
        })}
        <footer className="home-footer"><img src={redPandaMark} alt="" width={18} height={18} /> RED PANDA <span>与你的 Agent，一起把事情做好。</span></footer>
      </div></div>
      <CreateWorkspaceModal opened={workspaceOpened} onClose={() => setWorkspaceOpened(false)} />
      <CreateAgentModal opened={creatingIn !== null} workspaceId={creatingIn} workspaces={workspaces} connectionId={connectionId}
        onClose={() => setCreatingIn(null)} onCreated={(id) => { setCreatingIn(null); navigate(`/sessions/${encodeURIComponent(id)}`); }} />
    </section>
  );
}
