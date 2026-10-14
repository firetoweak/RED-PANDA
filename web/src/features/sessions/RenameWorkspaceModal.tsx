import { Alert, Button, Group, Modal, Stack, TextInput } from "@mantine/core";
import { useState } from "react";

import type { Workspace } from "../../api/contracts";
import { useRenameWorkspaceMutation } from "../../api/redpandaApi";

export function RenameWorkspaceModal({ workspace, onClose }: {
  workspace: Workspace;
  onClose: () => void;
}) {
  const [name, setName] = useState(workspace.name);
  const [renameWorkspace, { isLoading, isError }] = useRenameWorkspaceMutation();

  return (
    <Modal
      opened
      onClose={onClose}
      title="重命名工作区"
      closeOnClickOutside={!isLoading}
      closeOnEscape={!isLoading}
      withCloseButton={!isLoading}
    >
      <form onSubmit={(event) => {
        event.preventDefault();
        void renameWorkspace({ workspaceId: workspace.workspace_id, name: name.trim() })
          .unwrap().then(onClose, () => undefined);
      }}>
        <Stack gap="sm">
          <TextInput
            data-autofocus
            label="名称"
            description="只修改显示名称，工作区目录保持不变。"
            value={name}
            onChange={(event) => setName(event.currentTarget.value)}
            disabled={isLoading}
            required
          />
          {isError ? (
            <Alert color="red" title="重命名失败">未能保存名称，请重试。</Alert>
          ) : null}
          <Group gap="sm" justify="flex-end">
            <Button disabled={isLoading} onClick={onClose} variant="default">取消</Button>
            <Button type="submit" loading={isLoading} disabled={name.trim() === ""}>
              保存
            </Button>
          </Group>
        </Stack>
      </form>
    </Modal>
  );
}
