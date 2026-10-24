import { Button, Group, Popover, Stack, Text, TextInput, UnstyledButton } from "@mantine/core";
import { IconPencil } from "@tabler/icons-react";
import { useState } from "react";
import { useRenameAgentMutation } from "../../api/kondenApi";
import type { AgentRosterEntry } from "../../api/contracts";

export function EditableAgentName({ agent, connectionId, size }: {
  agent: AgentRosterEntry; connectionId: string | null; size: "sm" | "md";
}) {
  const [opened, setOpened] = useState(false);
  const [name, setName] = useState(agent.name);
  const [error, setError] = useState<string | null>(null);
  const [rename, { isLoading }] = useRenameAgentMutation();
  const valid = name.trim() !== "" && name === name.trim() && name !== agent.name;

  async function save() {
    if (connectionId === null || !valid || isLoading) return;
    setError(null);
    try {
      await rename({ connectionId, sessionId: agent.session_id, name }).unwrap();
      setOpened(false);
    } catch (cause) {
      setError(typeof cause === "string" ? cause : "改名失败，请重试");
    }
  }

  return <Popover opened={opened} onChange={(value) => { if (!isLoading) setOpened(value); }}
    position="bottom-start" width={280} trapFocus withinPortal={false}>
    <Popover.Target>
      <UnstyledButton className="editable-agent-name" aria-label={`修改 ${agent.name} 的名字`}
        disabled={connectionId === null} onClick={() => {
          setName(agent.name); setError(null); setOpened((value) => !value);
        }}>
        <Text component="span" fw={600} size={size} truncate>{agent.name}</Text>
        <IconPencil size={14} aria-hidden="true" />
      </UnstyledButton>
    </Popover.Target>
    <Popover.Dropdown>
      <form onSubmit={(event) => { event.preventDefault(); void save(); }}>
        <Stack gap="sm">
          <TextInput data-autofocus label="Bot 名字" value={name} disabled={isLoading}
            description="名字唯一，区分大小写，不能带首尾空白"
            error={error} onChange={(event) => { setName(event.currentTarget.value); setError(null); }}
            onKeyDown={(event) => {
              if (event.key === "Enter" && event.nativeEvent.isComposing) event.preventDefault();
            }} />
          <Group justify="flex-end" gap="xs">
            <Button variant="subtle" color="gray" size="xs" disabled={isLoading} onClick={() => setOpened(false)}>取消</Button>
            <Button type="submit" size="xs" loading={isLoading} disabled={!valid || connectionId === null}>保存</Button>
          </Group>
        </Stack>
      </form>
    </Popover.Dropdown>
  </Popover>;
}
