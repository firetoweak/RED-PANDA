import { ActionIcon, Alert, Group, Menu, Text, Tooltip, UnstyledButton } from "@mantine/core";
import { IconArchive, IconDots, IconFolder, IconPencil, IconPlus, IconTrash } from "@tabler/icons-react";
import { useState } from "react";

import { useArchiveSessionMutation } from "../../api/redpandaApi";
import type { WorkspaceGroup } from "./workspaces";

export function WorkspaceHeader({
  workspace, collapsed, connectionId, draftId, registered,
  onToggle, onCreate, onRename, onRemove, onArchived,
}: {
  workspace: WorkspaceGroup;
  collapsed: boolean;
  connectionId: string | null;
  draftId: string | undefined;
  registered: boolean;
  onToggle: () => void;
  onCreate: () => void;
  onRename: () => void;
  onRemove: () => void;
  onArchived: (sessionId: string) => void;
}) {
  const [opened, setOpened] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const [archiveError, setArchiveError] = useState(false);
  const [archiveSession] = useArchiveSessionMutation();
  const sessionIds = new Set(workspace.sessions.map((session) => session.session_id));
  if (draftId !== undefined) sessionIds.add(draftId);

  async function archiveAll() {
    if (connectionId === null) return;
    setArchiving(true);
    setArchiveError(false);
    try {
      for (const sessionId of sessionIds) {
        await archiveSession({ connectionId, sessionId }).unwrap();
        onArchived(sessionId);
      }
    } catch {
      setArchiveError(true);
    } finally {
      setArchiving(false);
    }
  }

  return (
    <>
      <Group
        className="workspace-head"
        gap={0}
        wrap="nowrap"
        onContextMenu={(event) => {
          event.preventDefault();
          setOpened(true);
        }}
      >
        <UnstyledButton
          aria-expanded={!collapsed}
          className="workspace-toggle"
          onClick={onToggle}
        >
          <Group gap={6} wrap="nowrap">
            <IconFolder size={14} />
            <Text className="workspace-name" fw={600} fz={12} truncate>{workspace.name}</Text>
          </Group>
        </UnstyledButton>
        <Tooltip label="在此工作区新建会话" openDelay={400} position="right">
          <ActionIcon
            aria-label={`在 ${workspace.name} 新建会话`}
            className="workspace-add"
            disabled={connectionId === null}
            onClick={onCreate}
            size="sm"
            variant="subtle"
          >
            <IconPlus size={14} />
          </ActionIcon>
        </Tooltip>
        <Menu opened={opened} onChange={setOpened} position="bottom-end" withinPortal>
          <Menu.Target>
            <ActionIcon
              aria-label={`${workspace.name} 工作区操作`}
              className="workspace-menu"
              loading={archiving}
              size="sm"
              variant="subtle"
            >
              <IconDots size={14} />
            </ActionIcon>
          </Menu.Target>
          <Menu.Dropdown>
            <Menu.Item leftSection={<IconPencil size={15} />} disabled={!registered || archiving} onClick={onRename}>
              重命名工作区
            </Menu.Item>
            <Menu.Item
              leftSection={<IconArchive size={15} />}
              disabled={connectionId === null || sessionIds.size === 0 || archiving}
              onClick={() => { void archiveAll(); }}
            >
              归档所有会话
            </Menu.Item>
            <Menu.Divider />
            <Menu.Item leftSection={<IconTrash size={15} />} color="red" disabled={!registered || archiving} onClick={onRemove}>
              移除工作区
            </Menu.Item>
          </Menu.Dropdown>
        </Menu>
      </Group>
      {archiveError ? (
        <Alert color="red" title="归档失败" mx={8} my={4}>
          部分会话未能归档，请重试。
        </Alert>
      ) : null}
    </>
  );
}
