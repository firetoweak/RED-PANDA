import { Alert, Button, Modal, Stack } from "@mantine/core";
import { useState } from "react";

import { useLinkProjectMutation } from "../../api/redpandaApi";
import type { AgentRosterEntry, Workspace } from "../../api/contracts";
import { ProjectDirectoryInput } from "./ProjectDirectoryInput";

export function LinkProjectModal({ opened, agent, workspaces, connectionId, onClose }: {
  opened: boolean;
  agent: AgentRosterEntry;
  workspaces: Workspace[];
  connectionId: string | null;
  onClose: () => void;
}) {
  const linked = new Set(agent.projects.map((project) => project.workspace_id));
  const available = workspaces.filter((workspace) => !linked.has(workspace.workspace_id));
  const [path, setPath] = useState("");
  const [selecting, setSelecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [linkProject, { isLoading, reset }] = useLinkProjectMutation();
  const ready = connectionId !== null && path.trim() !== "" && !selecting && !isLoading;

  function close() {
    setPath("");
    setError(null);
    reset();
    onClose();
  }

  async function submit() {
    if (!ready || connectionId === null) return;
    setError(null);
    try {
      await linkProject({ connectionId, sessionId: agent.session_id, workspaceId: null, path: path.trim() }).unwrap();
      close();
    } catch (cause) {
      setError(typeof cause === "string" ? cause : "关联项目失败");
    }
  }

  return (
    <Modal onClose={close} opened={opened} title={`给 ${agent.name} 关联代码项目`}
      closeOnClickOutside={!selecting && !isLoading} closeOnEscape={!selecting && !isLoading}
      withCloseButton={!selecting && !isLoading}>
      <Stack gap="sm">
        <ProjectDirectoryInput label="代码目录" value={path} onChange={setPath} workspaces={available}
          disabled={isLoading} onSelectingChange={setSelecting} />
        {error === null ? null : <Alert color="red" title="无法关联">{error}</Alert>}
        <Button loading={isLoading} disabled={!ready} onClick={() => void submit()}>关联</Button>
      </Stack>
    </Modal>
  );
}
