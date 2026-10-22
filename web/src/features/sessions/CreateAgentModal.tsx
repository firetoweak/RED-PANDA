import { Alert, Button, Group, Modal, Select, Stack, TextInput } from "@mantine/core";
import { IconFolderPlus } from "@tabler/icons-react";
import { useEffect, useState } from "react";

import { useCreateAgentMutation } from "../../api/redpandaApi";
import type { Workspace } from "../../api/contracts";
import { CreateWorkspaceModal } from "./CreateWorkspaceModal";

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
  const [addingProject, setAddingProject] = useState(false);
  const [createdProject, setCreatedProject] = useState<Workspace | null>(null);
  const [createAgent, { isLoading, reset }] = useCreateAgentMutation();
  const [error, setError] = useState<string | null>(null);
  const projectId = selectedId;
  const canSubmit = connectionId !== null && name.trim() !== "";
  const projects = createdProject !== null && !workspaces.some((item) => item.workspace_id === createdProject.workspace_id)
    ? [...workspaces, createdProject] : workspaces;

  useEffect(() => {
    if (!opened) {
      return;
    }
    setSelectedId(workspaceId);
    setAddingProject(false);
    setCreatedProject(null);
    setName("");
    setError(null);
  }, [opened, workspaceId]);

  function close() {
    setName("");
    setError(null);
    reset();
    onClose();
  }

  async function submit() {
    if (connectionId === null || name.trim() === "") {
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
    <>
      <Modal onClose={close} opened={opened && !addingProject} title="新建 Bot">
        <Stack gap="sm">
          <Group align="flex-end" gap="sm" wrap="nowrap">
            <Select
              label="代码项目（可选）" placeholder="不关联项目" clearable
              searchable nothingFoundMessage="没有匹配的代码项目"
              disabled={isLoading}
              style={{ flex: 1, minWidth: 0 }}
              data={projects.map((workspace) => ({
                value: workspace.workspace_id,
                label: workspace.name,
              }))}
              value={selectedId}
              onChange={setSelectedId}
              allowDeselect
            />
            <Button variant="light" disabled={isLoading} leftSection={<IconFolderPlus size={16} />}
              onClick={() => setAddingProject(true)}>添加代码项目</Button>
          </Group>
          <TextInput
            label="名字"
            description="名字唯一，区分大小写，不能带首尾空白；自动准备独立工作目录"
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
      <CreateWorkspaceModal opened={opened && addingProject} onClose={() => setAddingProject(false)}
        onCreated={(workspace) => {
          setCreatedProject(workspace);
          setSelectedId(workspace.workspace_id);
        }} />
    </>
  );
}
