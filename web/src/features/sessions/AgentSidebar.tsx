import { ActionIcon, AppShell, Button, Group, ScrollArea, SegmentedControl, Stack, Text, TextInput, Tooltip, UnstyledButton } from "@mantine/core";
import { IconFolder, IconX, IconAdjustments, IconPlus, IconSearch, IconUsersGroup } from "@tabler/icons-react";
import { useEffect, useRef, useState } from "react";
import { useMatch, useNavigate } from "react-router-dom";
import { useGetAgentsQuery, useGetGroupsQuery, useGetInboxQuery, useGetWorkspacesQuery } from "../../api/redpandaApi";
import { useAppSelector } from "../../app/hooks";
import { ColorSchemeSwitcher } from "../../app/ColorSchemeSwitcher";
import { CreateGroupModal } from "../groups/CreateGroupModal";
import { CreateAgentModal } from "./CreateAgentModal";
import { BotRoster } from "./BotRoster";
import { AutoAuthorize } from "./AutoAuthorize";

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
  const [sectionCreating, setSectionCreating] = useState(false);
  const [filter, setFilter] = useState("all");
  const searchInput = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const focusSearch = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault(); searchInput.current?.focus();
      }
    };
    window.addEventListener("keydown", focusSearch);
    return () => window.removeEventListener("keydown", focusSearch);
  }, []);
  const query = search.trim().toLocaleLowerCase();
  const open = (path: string) => { navigate(path); onNavigate(); };
  return <Stack h="100%" gap="md" className="sidebar-content">
    <AppShell.Section>
      <Button className="sidebar-create" fullWidth variant="default" leftSection={<IconPlus size={16} />} disabled={connectionId === null} onClick={() => setCreating(true)}>新建 Bot</Button>
      <TextInput ref={searchInput} className="sidebar-search" aria-label="搜索 Bot 或群聊" placeholder="搜索 Bot 或群聊" value={search} mt={14} size="sm" leftSection={<IconSearch size={14} />} rightSection={search === "" ? <kbd>Ctrl K</kbd> : <ActionIcon aria-label="清除搜索" size="xs" onClick={() => setSearch("")}><IconX size={12} /></ActionIcon>} rightSectionWidth={48} onChange={(event) => setSearch(event.currentTarget.value)} />
    </AppShell.Section>
    <AppShell.Section grow className="session-section">
      <Group justify="space-between" px={8} mb={8}><UnstyledButton className="sidebar-home" aria-label="所有对话" onClick={() => open("/")}><Text fw={600} size="xs">对话</Text></UnstyledButton><Group gap={2}>
        <Tooltip label="新建分组"><ActionIcon aria-label="新建分组" variant="subtle" color="gray" size={25} onClick={() => setSectionCreating(true)}><IconFolder size={15} /></ActionIcon></Tooltip>
        <Tooltip label="新建群聊"><ActionIcon aria-label="新建群聊" variant="subtle" color="gray" size={25} disabled={connectionId === null || agents.length < 2} onClick={() => setGroupCreating(true)}><IconUsersGroup size={16} /></ActionIcon></Tooltip>
      </Group></Group>
      <SegmentedControl className="sidebar-filter" aria-label="对话类型" fullWidth size="xs" mb={10} value={filter} onChange={setFilter} data={[{ value: "all", label: "全部" }, { value: "groups", label: "群聊" }, { value: "bots", label: "Bot" }]} />
      <ScrollArea className="session-scroll" type="hover"><BotRoster agents={agents.filter((agent) => filter !== "groups" && `${agent.name} ${agent.direction ?? ""}`.toLocaleLowerCase().includes(query))} chats={chats.filter((chat) => filter !== "bots" && chat.name.toLocaleLowerCase().includes(query))} attentionSessionIds={new Set(inbox.map((item) => item.session_id))} sessionId={sessionId} groupId={groupId} searching={query !== "" || filter !== "all"} onOpen={open} creatingSection={sectionCreating} onCloseSection={() => setSectionCreating(false)} /></ScrollArea>
    </AppShell.Section>
    <AppShell.Section className="sidebar-footer"><Group gap={8}><Button variant="subtle" leftSection={<IconAdjustments size={17} />} onClick={() => open("/settings/models")}>模型配置</Button><ColorSchemeSwitcher /><AutoAuthorize /></Group><Text size="xs" c="dimmed" px={8}>{connectionId === null ? "正在连接…" : "已连接"}</Text></AppShell.Section>
    <CreateAgentModal opened={creating} workspaceId={null} workspaces={workspaces} connectionId={connectionId} onClose={() => setCreating(false)} onCreated={(id) => { setCreating(false); open(`/sessions/${encodeURIComponent(id)}`); }} />
    <CreateGroupModal opened={groupCreating} agents={agents} connectionId={connectionId} onClose={() => setGroupCreating(false)} onCreated={(id) => { setGroupCreating(false); open(`/groups/${encodeURIComponent(id)}`); }} />
  </Stack>;
}
