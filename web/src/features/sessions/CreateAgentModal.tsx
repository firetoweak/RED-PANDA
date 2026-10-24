import { Alert, Button, Modal, Stack, TextInput } from "@mantine/core";
import { useState } from "react";

import { useCreateAgentMutation } from "../../api/kondenApi";
import type { Workspace } from "../../api/contracts";
import { ProjectDirectoryInput } from "./ProjectDirectoryInput";

export function CreateAgentModal({
  opened, workspaces, connectionId, onClose, onCreated,
}: {
  opened: boolean;
  workspaces: Workspace[];
  connectionId: string | null;
  onClose: () => void;
  onCreated: (sessionId: string) => void;
}) {
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [selecting, setSelecting] = useState(false);
  const [createAgent, { isLoading, reset }] = useCreateAgentMutation();
  const [error, setError] = useState<string | null>(null);
  const canSubmit = connectionId !== null && name.trim() !== "" && !selecting && !isLoading;

  function clearDraft() {
    setName("");
    setPath("");
    setError(null);
  }

  function close() {
    clearDraft();
    reset();
    onClose();
  }

  async function submit() {
    if (!canSubmit || connectionId === null) return;
    setError(null);
    try {
      const agent = await createAgent({ connectionId, path: path.trim() || null, name }).unwrap();
      clearDraft();
      onCreated(agent.session_id);
    } catch (cause) {
      setError(typeof cause === "string" ? cause : "创建 Bot 失败");
    }
  }

  return (
    <Modal onClose={close} opened={opened} title="新建 Bot"
      closeOnClickOutside={!selecting && !isLoading} closeOnEscape={!selecting && !isLoading}
      withCloseButton={!selecting && !isLoading}>
      <Stack gap="sm">
        <TextInput label="名字" description="名字唯一，区分大小写，不能带首尾空白；自动准备独立工作目录"
          disabled={isLoading || selecting} value={name} onChange={(event) => setName(event.currentTarget.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.nativeEvent.isComposing) {
              event.preventDefault();
              void submit();
            }
          }} />
        <ProjectDirectoryInput label="代码目录（可选）" value={path} onChange={setPath}
          workspaces={workspaces} disabled={isLoading} onSelectingChange={setSelecting} />
        {error === null ? null : <Alert color="red" title="无法创建">{error}</Alert>}
        <Button loading={isLoading} disabled={!canSubmit} onClick={() => void submit()}>创建</Button>
      </Stack>
    </Modal>
  );
}
