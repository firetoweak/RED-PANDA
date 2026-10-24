import { Alert, Button, Modal, MultiSelect, Stack, TextInput } from "@mantine/core";
import { useEffect, useState } from "react";

import type { AgentRosterEntry } from "../../api/contracts";
import { useCreateGroupMutation } from "../../api/kondenApi";

const MIN_MEMBERS = 2;
const MAX_MEMBERS = 6;

export function CreateGroupModal({
  opened, agents, connectionId, onClose, onCreated,
}: {
  opened: boolean;
  agents: AgentRosterEntry[];
  connectionId: string | null;
  onClose: () => void;
  onCreated: (groupId: string) => void;
}) {
  const [name, setName] = useState("");
  const [members, setMembers] = useState<string[]>([]);
  const [createGroup, { isLoading, reset }] = useCreateGroupMutation();
  const [error, setError] = useState<string | null>(null);
  const candidates = agents;
  const canSubmit = connectionId !== null && name.trim() !== ""
    && members.length >= MIN_MEMBERS && members.length <= MAX_MEMBERS;

  useEffect(() => {
    if (!opened) {
      return;
    }
    setName("");
    setMembers([]);
    setError(null);
  }, [opened]);

  function close() {
    reset();
    onClose();
  }

  async function submit() {
    if (!canSubmit || connectionId === null) {
      return;
    }
    setError(null);
    try {
      const group = await createGroup({ connectionId, name, members }).unwrap();
      onCreated(group.group_id);
    } catch (cause) {
      setError(typeof cause === "string" ? cause : "创建群聊失败");
    }
  }

  return (
    <Modal onClose={close} opened={opened} title="新建群聊">
      <Stack gap="sm">
        <TextInput
          label="群名"
          description="群名唯一，不能带首尾空白"
          value={name}
          onChange={(event) => setName(event.currentTarget.value)}
        />
        <MultiSelect
          label="成员"
          description={`选择 ${MIN_MEMBERS} 到 ${MAX_MEMBERS} 个 Agent`}
          data={candidates.map((agent) => agent.name)}
          value={members}
          onChange={setMembers}
          maxValues={MAX_MEMBERS}
          nothingFoundMessage="还没有 Agent"
          searchable
        />
        {error === null ? null : (
          <Alert color="red" title="无法创建">
            {error}
          </Alert>
        )}
        <Button loading={isLoading} disabled={!canSubmit} onClick={() => void submit()}>
          创建
        </Button>
      </Stack>
    </Modal>
  );
}
