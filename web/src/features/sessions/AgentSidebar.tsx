import { ActionIcon, AppShell, Group, ScrollArea, SegmentedControl, Stack, Text, TextInput, Tooltip, VisuallyHidden } from "@mantine/core";
import { IconFolderPlus, IconMessages, IconPlugConnected, IconPlugConnectedX, IconRobot, IconSearch, IconUserPlus, IconUsersGroup, IconUsersPlus, IconX } from "@tabler/icons-react";
import { useEffect, useRef, useState } from "react";
import { useMatch, useNavigate } from "react-router-dom";
import { useGetAgentsQuery, useGetGroupsQuery, useGetInboxQuery, useGetWorkspacesQuery } from "../../api/redpandaApi";
import { useAppSelector } from "../../app/hooks";
import { ColorSchemeSwitcher } from "../../app/ColorSchemeSwitcher";
import { CreateGroupModal } from "../groups/CreateGroupModal";
import { CreateAgentModal } from "./CreateAgentModal";
import { BotRoster } from "./BotRoster";
import { AutoAuthorize } from "./AutoAuthorize";
import { SettingsMenu } from "../settings/SettingsMenu";

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
      <Group className="sidebar-toolbar" justify="space-between" gap={8}>
        <Tooltip label="所有对话" events={{ hover: true, focus: true, touch: false }}>
          <ActionIcon aria-label="所有对话" variant="subtle" color="gray" size={32} onClick={() => open("/")}><IconMessages size={19} /></ActionIcon>
        </Tooltip>
        <Group gap={4}>
          <Tooltip label="新建 Bot" events={{ hover: true, focus: true, touch: false }}>
            <ActionIcon className="sidebar-create" aria-label="新建 Bot" variant="subtle" size={32} disabled={connectionId === null} onClick={() => setCreating(true)}><IconUserPlus size={19} /></ActionIcon>
          </Tooltip>
          <Tooltip label="新建群聊" events={{ hover: true, focus: true, touch: false }}>
            <ActionIcon aria-label="新建群聊" variant="subtle" color="gray" size={32} disabled={connectionId === null || agents.length < 2} onClick={() => setGroupCreating(true)}><IconUsersPlus size={19} /></ActionIcon>
          </Tooltip>
          <Tooltip label="新建分组" events={{ hover: true, focus: true, touch: false }}>
            <ActionIcon aria-label="新建分组" variant="subtle" color="gray" size={32} onClick={() => setSectionCreating(true)}><IconFolderPlus size={19} /></ActionIcon>
          </Tooltip>
        </Group>
      </Group>
      <TextInput ref={searchInput} className="sidebar-search" aria-label="搜索 Bot 或群聊" placeholder="搜索 Bot 或群聊" value={search} mt={12} size="sm" leftSection={<IconSearch size={14} />} rightSection={search === "" ? <kbd>Ctrl K</kbd> : <ActionIcon aria-label="清除搜索" size="xs" onClick={() => setSearch("")}><IconX size={12} /></ActionIcon>} rightSectionWidth={48} onChange={(event) => setSearch(event.currentTarget.value)} />
    </AppShell.Section>
    <AppShell.Section grow className="session-section">
      <SegmentedControl className="sidebar-filter" aria-label="对话类型" fullWidth size="xs" mb={10} value={filter} onChange={setFilter} data={[
        { value: "all", label: <Tooltip label="全部"><span className="sidebar-filter-option"><IconMessages aria-hidden="true" size={17} /><VisuallyHidden>全部</VisuallyHidden></span></Tooltip> },
        { value: "groups", label: <Tooltip label="群聊"><span className="sidebar-filter-option"><IconUsersGroup aria-hidden="true" size={17} /><VisuallyHidden>群聊</VisuallyHidden></span></Tooltip> },
        { value: "bots", label: <Tooltip label="Bot"><span className="sidebar-filter-option"><IconRobot aria-hidden="true" size={17} /><VisuallyHidden>Bot</VisuallyHidden></span></Tooltip> },
      ]} />
      <ScrollArea className="session-scroll" type="hover"><BotRoster agents={agents.filter((agent) => filter !== "groups" && agent.name.toLocaleLowerCase().includes(query))} chats={chats.filter((chat) => filter !== "bots" && chat.name.toLocaleLowerCase().includes(query))} attentionSessionIds={new Set(inbox.map((item) => item.session_id))} sessionId={sessionId} groupId={groupId} searching={query !== "" || filter !== "all"} onOpen={open} creatingSection={sectionCreating} onCloseSection={() => setSectionCreating(false)} /></ScrollArea>
    </AppShell.Section>
    <AppShell.Section className="sidebar-footer"><Group gap={4} wrap="nowrap">
      <SettingsMenu onOpen={onNavigate} /><ColorSchemeSwitcher />
      <Tooltip label={connectionId === null ? "正在连接…" : "已连接"}>
        <span className="connection-status" data-connected={connectionId !== null} role="status" aria-label={connectionId === null ? "正在连接…" : "已连接"}>
          {connectionId === null ? <IconPlugConnectedX aria-hidden="true" size={16} /> : <IconPlugConnected aria-hidden="true" size={16} />}
        </span>
      </Tooltip>
      <AutoAuthorize />
    </Group>{connectionId === null ? <Text size="xs" c="dimmed" px={8}>正在连接…</Text> : null}</AppShell.Section>
    <CreateAgentModal opened={creating} workspaces={workspaces} connectionId={connectionId} onClose={() => setCreating(false)} onCreated={(id) => { setCreating(false); open(`/sessions/${encodeURIComponent(id)}`); }} />
    <CreateGroupModal opened={groupCreating} agents={agents} connectionId={connectionId} onClose={() => setGroupCreating(false)} onCreated={(id) => { setGroupCreating(false); open(`/groups/${encodeURIComponent(id)}`); }} />
  </Stack>;
}
