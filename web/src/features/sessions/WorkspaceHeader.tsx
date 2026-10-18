import { ActionIcon, Group, Menu, Text, Tooltip, UnstyledButton } from "@mantine/core";
import { IconDots, IconFolder, IconPencil, IconPlus, IconTrash } from "@tabler/icons-react";
import { useState } from "react";

export function WorkspaceHeader({
  name, collapsed, connectionId, registered,
  onToggle, onCreate, onRename, onRemove,
}: {
  name: string;
  collapsed: boolean;
  connectionId: string | null;
  registered: boolean;
  onToggle: () => void;
  onCreate: () => void;
  onRename: () => void;
  onRemove: () => void;
}) {
  const [opened, setOpened] = useState(false);

  return (
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
          <Text className="workspace-name" fw={600} fz={12} truncate>{name}</Text>
        </Group>
      </UnstyledButton>
      <Tooltip label="在此项目新建 Agent" openDelay={400} position="right">
        <ActionIcon
          aria-label={`在 ${name} 新建 Agent`}
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
            aria-label={`${name} 工作区操作`}
            className="workspace-menu"
            size="sm"
            variant="subtle"
          >
            <IconDots size={14} />
          </ActionIcon>
        </Menu.Target>
        <Menu.Dropdown>
          <Menu.Item leftSection={<IconPencil size={15} />} disabled={!registered} onClick={onRename}>
            重命名工作区
          </Menu.Item>
          <Menu.Divider />
          <Menu.Item leftSection={<IconTrash size={15} />} color="red" disabled={!registered} onClick={onRemove}>
            移除工作区
          </Menu.Item>
        </Menu.Dropdown>
      </Menu>
    </Group>
  );
}
