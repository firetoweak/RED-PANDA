import { ActionIcon, AppShell, Button, Group, ScrollArea, Stack, Text, TextInput, Tooltip, UnstyledButton } from "@mantine/core";
import { IconX, IconAdjustments, IconPlus, IconSearch, IconUsersGroup } from "@tabler/icons-react";
import { useState } from "react";
import { useMatch, useNavigate } from "react-router-dom";
import { useGetAgentsQuery, useGetGroupsQuery, useGetInboxQuery, useGetWorkspacesQuery } from "../../api/redpandaApi";
import { useAppSelector } from "../../app/hooks";
import { redPandaMark } from "../../app/brand";
import { ColorSchemeSwitcher } from "../../app/ColorSchemeSwitcher";
import { CreateGroupModal } from "../groups/CreateGroupModal";
import { CreateAgentModal } from "./CreateAgentModal";
import { BotRoster } from "./BotRoster";

export function AgentSidebar({ onNavigate }: { onNavigate: () => void }) {
  const navigate = useNavigate();
  const sessionId = useMatch("/sessions/:sessionId")?.params.sessionId;
  const groupId = useMatch("/groups/:groupId")?.params.groupId;
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const { data: agents = [] } = useGetAgentsQuery();
  const { data: chats = [] } = useGetGroupsQuery();
  const { data: inbox = [] } = useGetInboxQuery();
  const { data: workspaces = [] } = useGetWorkspacesQuery();
  const [search, setSearch] = useState("");
  const [creating, setCreating] = useState(false);
  const [groupCreating, setGroupCreating] = useState(false);
  const query = search.trim().toLocaleLowerCase();
  const open = (path: string) => { navigate(path); onNavigate(); };
  return <Stack h="100%" gap="md" className="sidebar-content">
    <AppShell.Section>
      <UnstyledButton className="sidebar-brand" aria-label="RED PANDA 首页" onClick={() => open("/")}>
        <Group gap="sm" px={6} py={4}><img src={redPandaMark} alt="" width={32} height={32} /><Text fw={700}>RED PANDA</Text></Group>
      </UnstyledButton>
      <TextInput aria-label="搜索 Bot 或群聊" placeholder="搜索 Bot 或群聊" value={search} mt="sm" size="xs" leftSection={<IconSearch size={14} />} rightSection={search === "" ? null : <ActionIcon aria-label="清除搜索" size="xs" onClick={() => setSearch("")}><IconX size={12} /></ActionIcon>} onChange={(event) => setSearch(event.currentTarget.value)} />
    </AppShell.Section>
    <AppShell.Section grow className="session-section">
      <Group justify="space-between" px={8}><Text c="dimmed" size="xs">Bot 与群聊</Text><Group gap={2}>
        <Tooltip label="新建 Bot"><ActionIcon aria-label="新建 Bot" variant="subtle" disabled={connectionId === null} onClick={() => setCreating(true)}><IconPlus size={16} /></ActionIcon></Tooltip>
        <Tooltip label="新建群聊"><ActionIcon aria-label="新建群聊" variant="subtle" disabled={connectionId === null || agents.length < 2} onClick={() => setGroupCreating(true)}><IconUsersGroup size={16} /></ActionIcon></Tooltip>
      </Group></Group>
      <ScrollArea className="session-scroll" type="hover"><BotRoster agents={agents.filter((agent) => `${agent.name} ${agent.direction ?? ""}`.toLocaleLowerCase().includes(query))} chats={chats.filter((chat) => chat.name.toLocaleLowerCase().includes(query))} attentionSessionIds={new Set(inbox.map((item) => item.session_id))} sessionId={sessionId} groupId={groupId} searching={query !== ""} onOpen={open} /></ScrollArea>
    </AppShell.Section>
    <AppShell.Section className="sidebar-footer"><Group gap={8}><Button variant="subtle" leftSection={<IconAdjustments size={17} />} onClick={() => open("/settings/models")}>模型配置</Button><ColorSchemeSwitcher /></Group><Text size="xs" c="dimmed" px={8}>{connectionId === null ? "正在连接…" : "已连接"}</Text></AppShell.Section>
    <CreateAgentModal opened={creating} workspaceId={null} workspaces={workspaces} connectionId={connectionId} onClose={() => setCreating(false)} onCreated={(id) => { setCreating(false); open(`/sessions/${encodeURIComponent(id)}`); }} />
    <CreateGroupModal opened={groupCreating} agents={agents} connectionId={connectionId} onClose={() => setGroupCreating(false)} onCreated={(id) => { setGroupCreating(false); open(`/groups/${encodeURIComponent(id)}`); }} />
  </Stack>;
}
