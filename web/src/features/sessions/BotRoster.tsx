import { ActionIcon, Button, Collapse, Group, Menu, Modal, Stack, Text, TextInput, Tooltip, UnstyledButton } from "@mantine/core";
import { useLocalStorage } from "@mantine/hooks";
import { IconCheck, IconChevronDown, IconChevronRight, IconDots, IconFolderPlus, IconPencil, IconTrash, IconUsersGroup } from "@tabler/icons-react";
import { useState, type ReactNode } from "react";
import { z } from "zod";

import type { AgentRosterEntry, GroupSummary } from "../../api/contracts";
import { useAppSelector } from "../../app/hooks";
import { AgentStatus } from "./AgentStatus";
import { BotAvatar } from "../conversation/ChatPresentation";
import { RemoveConversationModal, type RemovalTarget } from "./RemoveConversationModal";

const organizationSchema = z.object({
  sections: z.array(z.object({ id: z.string(), name: z.string(), collapsed: z.boolean() })),
  assignments: z.record(z.string(), z.string()),
  ungroupedCollapsed: z.boolean(),
});
type Organization = z.infer<typeof organizationSchema>;
type Section = Organization["sections"][number];
type Editing = { id: string | null; name: string; entry?: string };
const EMPTY_ORGANIZATION: Organization = { sections: [], assignments: {}, ungroupedCollapsed: false };

function deserializeOrganization(value: string | undefined): Organization {
  try {
    const result = organizationSchema.safeParse(JSON.parse(value ?? "null"));
    return result.success ? result.data : EMPTY_ORGANIZATION;
  } catch (error) {
    if (error instanceof SyntaxError) return EMPTY_ORGANIZATION;
    throw error;
  }
}

/** 分组只整理当前浏览器的名册，不改变身份或群成员。 */
export function BotRoster({ agents, chats, attentionSessionIds, sessionId, groupId, searching, onOpen, creatingSection = false, onCloseSection }: {
  agents: AgentRosterEntry[];
  chats: GroupSummary[];
  attentionSessionIds: ReadonlySet<string>;
  sessionId?: string;
  groupId?: string;
  searching: boolean;
  onOpen: (path: string) => void;
  creatingSection?: boolean;
  onCloseSection?: () => void;
}) {
  const [organization, setOrganization] = useLocalStorage<Organization>({
    key: "konden.sidebarGroups",
    defaultValue: EMPTY_ORGANIZATION,
    getInitialValueInEffect: false,
    deserialize: deserializeOrganization,
  });
  const [editing, setEditing] = useState<Editing | null>(null);
  const [removing, setRemoving] = useState<RemovalTarget | null>(null);
  const activeEditing = editing ?? (creatingSection ? { id: null, name: "" } as Editing : null);
  const closeEditing = () => { setEditing(null); onCloseSection?.(); };
  const assignedSection = (entry: string) => {
    const id = organization.assignments[entry];
    return organization.sections.some((section) => section.id === id) ? id : null;
  };

  function move(entry: string, sectionId: string | null) {
    setOrganization((current) => {
      const assignments = { ...current.assignments };
      if (sectionId === null) delete assignments[entry];
      else assignments[entry] = sectionId;
      return { ...current, assignments, ungroupedCollapsed: sectionId === null ? false : current.ungroupedCollapsed,
        sections: current.sections.map((section) => section.id === sectionId ? { ...section, collapsed: false } : section) };
    });
  }

  function save(name: string) {
    if (activeEditing === null) return;
    const id = activeEditing.id ?? crypto.randomUUID();
    setOrganization((current) => ({ ...current,
      sections: activeEditing.id === null ? [...current.sections, { id, name, collapsed: false }]
        : current.sections.map((section) => section.id === id ? { ...section, name } : section),
      assignments: activeEditing.entry === undefined ? current.assignments : { ...current.assignments, [activeEditing.entry]: id },
    }));
    closeEditing();
  }

  function remove(id: string) {
    setOrganization((current) => ({ ...current, ungroupedCollapsed: false,
      sections: current.sections.filter((section) => section.id !== id),
      assignments: Object.fromEntries(Object.entries(current.assignments).filter(([, section]) => section !== id)),
    }));
  }

  function renderRow(entry: string, name: string, active: boolean, path: string, children: ReactNode) {
    return <RosterRow key={entry} name={name} active={active} onOpen={() => onOpen(path)}
      sections={organization.sections} sectionId={assignedSection(entry)} onMove={(id) => move(entry, id)}
      onCreate={() => setEditing({ id: null, name: "", entry })}
      removeLabel={entry.startsWith("agent:") ? "删除 Bot" : "解散群聊"}
      onRemove={() => setRemoving({ kind: entry.startsWith("agent:") ? "agent" : "group", id: entry.slice(entry.indexOf(":") + 1), name: entry.startsWith("chat:") ? name.slice(3) : name })}>{children}</RosterRow>;
  }

  function renderSection(section: Section | null) {
    const sectionAgents = agents.filter((agent) => assignedSection(`agent:${agent.session_id}`) === (section?.id ?? null));
    const sectionChats = chats.filter((chat) => assignedSection(`chat:${chat.group_id}`) === (section?.id ?? null));
    const count = sectionAgents.length + sectionChats.length;
    if (searching && count === 0) return null;
    const name = section?.name ?? "未分组";
    const collapsed = !searching && (section?.collapsed ?? organization.ungroupedCollapsed);
    return <section key={section?.id ?? "ungrouped"} className="roster-section" aria-label={`名册的分组 ${name}`}>
      <Group className="roster-section-header" gap={2} wrap="nowrap">
        <UnstyledButton className="roster-section-toggle" aria-label={`分组 ${name}`} aria-expanded={!collapsed}
          onClick={() => setOrganization((current) => section === null
            ? { ...current, ungroupedCollapsed: !current.ungroupedCollapsed }
            : { ...current, sections: current.sections.map((item) => item.id === section.id ? { ...item, collapsed: !item.collapsed } : item) })}>
          {collapsed ? <IconChevronRight size={12} /> : <IconChevronDown size={12} />}
          <Text fz={11} truncate>{name}</Text><span className="roster-section-count" aria-hidden="true">{count}</span>
        </UnstyledButton>
        {section === null ? null : <Menu position="bottom-end" withinPortal>
          <Menu.Target><ActionIcon className="roster-section-menu" aria-label={`${name} 分组操作`} variant="subtle" color="gray" size="xs"><IconDots size={13} /></ActionIcon></Menu.Target>
          <Menu.Dropdown>
            <Menu.Item leftSection={<IconPencil size={14} />} onClick={() => setEditing({ id: section.id, name })}>重命名分组</Menu.Item>
            <Menu.Item leftSection={<IconTrash size={14} />} onClick={() => remove(section.id)}>删除分组</Menu.Item>
          </Menu.Dropdown>
        </Menu>}
      </Group>
      <Collapse expanded={!collapsed}><Stack gap={3} className="roster-section-entries">
        {count === 0 ? <Text c="dimmed" fz={11} px={10} py={8}>{section === null ? "暂无 Bot 或群聊" : "从条目的菜单移入 Bot 或群聊"}</Text> : null}
        {sectionAgents.map((agent) => renderRow(`agent:${agent.session_id}`, agent.name, agent.session_id === sessionId,
          `/sessions/${encodeURIComponent(agent.session_id)}`, <Tooltip label={agent.name} openDelay={700} position="right">
            <Group gap={10} wrap="nowrap">
              <span className="sidebar-agent-avatar"><BotAvatar name={agent.name} sessionId={agent.session_id} className="agent-avatar-small" /><AgentStatus agent={agent} attention={attentionSessionIds.has(agent.session_id)} /></span>
              <Stack className="sidebar-agent-identity" gap={2}>
                <Text className="session-title" fz={13} truncate>{agent.name}</Text>
              </Stack>
              <UnreadBadge sessionId={agent.session_id} name={agent.name} />
            </Group>
          </Tooltip>))}
        {sectionChats.map((chat) => renderRow(`chat:${chat.group_id}`, `群聊 ${chat.name}`, chat.group_id === groupId,
          `/groups/${encodeURIComponent(chat.group_id)}`, <Group gap={10} wrap="nowrap">
            <span className="agent-avatar agent-avatar-small"><IconUsersGroup size={16} /></span>
            <Stack className="sidebar-agent-identity" gap={2}><Text className="session-title" fz={13} truncate>{chat.name}</Text><Text c="dimmed" fz={11}>群聊 · {chat.members.length} 位 Bot</Text></Stack>
          </Group>))}
      </Stack></Collapse>
    </section>;
  }

  return <div className="workspace-sessions">
    {removing === null ? null : <RemoveConversationModal target={removing} onClose={() => setRemoving(null)} onRemoved={() => {
      if (removing.kind === "agent" ? removing.id === sessionId : removing.id === groupId) onOpen("/");
      setRemoving(null);
    }} />}
    {onCloseSection === undefined ? <Group className="roster-sections-toolbar" justify="space-between" wrap="nowrap">
      <Text c="dimmed" fz={10}>分组</Text>
      <Tooltip label="新建分组" openDelay={400}><ActionIcon aria-label="新建分组" size="xs" variant="subtle" color="gray"
        onClick={() => setEditing({ id: null, name: "" })}><IconFolderPlus size={13} /></ActionIcon></Tooltip>
    </Group> : null}
    {organization.sections.map(renderSection)}
    {renderSection(null)}
    {activeEditing === null ? null : <SectionNameModal initialName={activeEditing.name} existing={organization.sections.filter((section) => section.id !== activeEditing.id).map((section) => section.name)}
      creating={activeEditing.id === null} onSave={save} onClose={closeEditing} />}
  </div>;
}

function RosterRow({ name, active, children, sections, sectionId, onOpen, onMove, onCreate, onRemove, removeLabel }: {
  name: string; active: boolean; children: ReactNode; sections: Section[]; sectionId: string | null;
  onOpen: () => void; onMove: (id: string | null) => void; onCreate: () => void; onRemove: () => void;
  removeLabel: string;
}) {
  const [opened, setOpened] = useState(false);
  return <Group className={active ? "session-row is-active" : "session-row"} gap={4} wrap="nowrap"
    onContextMenu={(event) => { event.preventDefault(); setOpened(true); }}>
    <UnstyledButton className="session-row-main" aria-label={name} aria-current={active ? "page" : undefined} onClick={onOpen}>{children}</UnstyledButton>
    <Menu opened={opened} onChange={setOpened} position="bottom-end" withinPortal>
      <Menu.Target><ActionIcon className="roster-entry-menu" aria-label={`${name} 操作`} color="gray" variant="subtle" size="xs"><IconDots size={13} /></ActionIcon></Menu.Target>
      <Menu.Dropdown>
        <Menu.Label>移动到分组</Menu.Label>
        <Menu.Item leftSection={sectionId === null ? <IconCheck size={14} /> : undefined} onClick={() => onMove(null)}>未分组</Menu.Item>
        {sections.map((section) => <Menu.Item key={section.id} leftSection={sectionId === section.id ? <IconCheck size={14} /> : undefined}
          onClick={() => onMove(section.id)}>{section.name}</Menu.Item>)}
        <Menu.Divider />
        <Menu.Item leftSection={<IconFolderPlus size={14} />} onClick={onCreate}>新建分组</Menu.Item>
        <Menu.Divider />
        <Menu.Item color="red" leftSection={<IconTrash size={14} />} onClick={onRemove}>{removeLabel}</Menu.Item>
      </Menu.Dropdown>
    </Menu>
  </Group>;
}

function UnreadBadge({ sessionId, name }: { sessionId: string; name: string }) {
  const unread = useAppSelector((state) => state.runtime.sessions[sessionId]?.unread ?? 0);
  return unread === 0 ? null : <span className="sidebar-unread" role="status" aria-label={`${name} 有 ${unread} 条未读消息`}>
    {unread > 99 ? "99+" : unread}
  </span>;
}

function SectionNameModal({ initialName, creating, existing, onSave, onClose }: {
  initialName: string; creating: boolean; existing: string[]; onSave: (name: string) => void; onClose: () => void;
}) {
  const [name, setName] = useState(initialName);
  const trimmed = name.trim();
  const duplicate = trimmed === "未分组" || existing.includes(trimmed);
  return <Modal opened title={creating ? "新建分组" : "重命名分组"} onClose={onClose}>
    <form onSubmit={(event) => { event.preventDefault(); if (trimmed !== "" && !duplicate) onSave(trimmed); }}>
      <Stack gap="md">
        <TextInput data-autofocus label="分组名称" placeholder="例如：开发、测试、协作群" value={name}
          onChange={(event) => setName(event.currentTarget.value)} error={duplicate ? "这个分组名称已存在" : undefined} />
        <Text c="dimmed" fz={12}>Bot 和群聊都可放入分组。删除分组只会将条目移回未分组。</Text>
        <Group justify="flex-end"><Button variant="default" onClick={onClose}>取消</Button>
          <Button type="submit" disabled={trimmed === "" || duplicate}>{creating ? "创建" : "保存"}</Button></Group>
      </Stack>
    </form>
  </Modal>;
}
