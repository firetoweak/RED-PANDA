import { Alert, Button, Modal, SegmentedControl, Select, Stack, TextInput } from "@mantine/core";
import { useEffect, useState } from "react";

import { useLinkProjectMutation } from "../../api/redpandaApi";
import type { AgentRosterEntry, Workspace } from "../../api/contracts";

export function LinkProjectModal({
  opened, agent, workspaces, connectionId, onClose,
}: {
  opened: boolean;
  agent: AgentRosterEntry;
  workspaces: Workspace[];
  connectionId: string | null;
  onClose: () => void;
}) {
  const linked = new Set(agent.projects.map((project) => project.workspace_id));
  const available = workspaces.filter((workspace) => !linked.has(workspace.workspace_id));
  const [source, setSource] = useState<"registered" | "path">(available.length > 0 ? "registered" : "path");
  const [workspaceId, setWorkspaceId] = useState<string | null>(null);
  const [path, setPath] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [linkProject, { isLoading, reset }] = useLinkProjectMutation();
  const ready = connectionId !== null && (source === "registered" ? workspaceId !== null : path.trim() !== "");

  useEffect(() => {
    if (!opened) return;
    setSource(available.length > 0 ? "registered" : "path");
    setWorkspaceId(null);
    setPath("");
    setError(null);
    reset();
    // Reset only when the dialog opens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opened]);

  async function submit() {
    if (!ready || connectionId === null) return;
    setError(null);
    try {
      await linkProject({
        connectionId, sessionId: agent.session_id,
        workspaceId: source === "registered" ? workspaceId : null,
        path: source === "path" ? path.trim() : null,
      }).unwrap();
      onClose();
    } catch (cause) {
      setError(typeof cause === "string" ? cause : "关联项目失败");
    }
  }

  return (
    <Modal onClose={onClose} opened={opened} title={`给 ${agent.name} 关联代码项目`}>
      <Stack gap="sm">
        <SegmentedControl value={source} onChange={(value) => setSource(value as "registered" | "path")}
          data={[{ value: "registered", label: "已登记的项目", disabled: available.length === 0 },
            { value: "path", label: "本地仓库路径" }]} />
        {source === "registered" ? (
          <Select label="项目" placeholder="选择项目" value={workspaceId} onChange={setWorkspaceId}
            data={available.map((workspace) => ({ value: workspace.workspace_id, label: workspace.name }))} />
        ) : (
          <TextInput label="仓库路径" description="Git 仓库根目录的绝对路径，至少有一个提交；会原地登记为项目"
            value={path} onChange={(event) => setPath(event.currentTarget.value)} />
        )}
        {error === null ? null : <Alert color="red" title="无法关联">{error}</Alert>}
        <Button loading={isLoading} disabled={!ready} onClick={() => void submit()}>关联</Button>
      </Stack>
    </Modal>
  );
}
