import { Alert, Button, Modal, Select, Stack, TextInput } from "@mantine/core";
import { useEffect, useState } from "react";

import { useCreateAgentMutation } from "../../api/redpandaApi";
import type { Workspace } from "../../api/contracts";

export function CreateAgentModal({
  opened, workspaceId, workspaces, connectionId, onClose, onCreated,
}: {
  opened: boolean;
  workspaceId: string | null;
  workspaces: Workspace[];
  connectionId: string | null;
  onClose: () => void;
  onCreated: (sessionId: string) => void;
}) {
  const [name, setName] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(workspaceId);
  const [createAgent, { isLoading, reset }] = useCreateAgentMutation();
  const [error, setError] = useState<string | null>(null);
  const projectId = selectedId;
  const fallbackId = workspaces[0]?.workspace_id ?? null;
  const canSubmit = connectionId !== null && projectId !== null && name.trim() !== "";

  useEffect(() => {
    if (!opened) {
      return;
    }
    setSelectedId(workspaceId ?? fallbackId);
    setName("");
    setError(null);
  }, [opened, workspaceId, fallbackId]);

  function close() {
    setName("");
    setError(null);
    reset();
    onClose();
  }

  async function submit() {
    if (connectionId === null || projectId === null || name.trim() === "") {
      return;
    }
    setError(null);
    try {
      const agent = await createAgent({
        connectionId,
        workspaceId: projectId,
        name,
      }).unwrap();
      setName("");
      onCreated(agent.session_id);
    } catch (cause) {
      setError(typeof cause === "string" ? cause : "创建 Agent 失败");
    }
  }

  return (
    <Modal onClose={close} opened={opened} title="新建 Agent">
      <Stack gap="sm">
        {workspaces.length > 1 ? (
          <Select
            label="项目"
            data={workspaces.map((workspace) => ({
              value: workspace.workspace_id,
              label: workspace.name,
            }))}
            value={selectedId}
            onChange={setSelectedId}
            allowDeselect={false}
          />
        ) : null}
        <TextInput
          label="名字"
          description="项目内唯一，区分大小写，不能带首尾空白"
          value={name}
          onChange={(event) => setName(event.currentTarget.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              void submit();
            }
          }}
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
