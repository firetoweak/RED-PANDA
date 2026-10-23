import { ActionIcon, Button, Group, Stack, Text, Textarea } from "@mantine/core";
import { IconPencil } from "@tabler/icons-react";
import { useState } from "react";
import { useSetAgentDirectionMutation } from "../../api/redpandaApi";
import type { AgentRosterEntry } from "../../api/contracts";

export function EditableAgentDirection({ agent, connectionId }: {
  agent: AgentRosterEntry; connectionId: string | null;
}) {
  const [editing, setEditing] = useState(false);
  const [direction, setDirection] = useState(agent.direction ?? "");
  const [error, setError] = useState<string | null>(null);
  const [update, { isLoading }] = useSetAgentDirectionMutation();
  const valid = direction.trim() !== "" && direction !== agent.direction;

  async function save() {
    if (connectionId === null || !valid || isLoading) return;
    setError(null);
    try {
      await update({ connectionId, sessionId: agent.session_id, direction }).unwrap();
      setEditing(false);
    } catch (cause) {
      setError(typeof cause === "string" ? cause : "保存失败，请重试");
    }
  }

  return <div className="agent-details-section">
    <Group gap="xs" justify="space-between">
      <Text className="agent-details-label" size="xs" c="dimmed">负责方向</Text>
      {editing ? null : <ActionIcon variant="subtle" color="gray" size="sm"
        aria-label={`修改 ${agent.name} 的负责方向`} disabled={connectionId === null}
        onClick={() => { setDirection(agent.direction ?? ""); setError(null); setEditing(true); }}>
        <IconPencil size={14} aria-hidden="true" />
      </ActionIcon>}
    </Group>
    {editing ? <form onSubmit={(event) => { event.preventDefault(); void save(); }}>
      <Stack gap="xs">
        <Textarea autoFocus autosize minRows={3} maxRows={8} aria-label="负责方向"
          description="你和 Bot 都可以调整，保存后同事也能看到"
          placeholder="一句话说明主要负责什么" value={direction} disabled={isLoading} error={error}
          onChange={(event) => { setDirection(event.currentTarget.value); setError(null); }}
          onKeyDown={(event) => { if (event.key === "Escape" && !isLoading) setEditing(false); }} />
        <Group justify="flex-end" gap="xs">
          <Button variant="subtle" color="gray" size="xs" disabled={isLoading} onClick={() => setEditing(false)}>取消</Button>
          <Button type="submit" size="xs" loading={isLoading} disabled={!valid || connectionId === null}>保存</Button>
        </Group>
      </Stack>
    </form> : <Text size="sm">{agent.direction ?? "尚未设置负责方向"}</Text>}
  </div>;
}
