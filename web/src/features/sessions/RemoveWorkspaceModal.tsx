import { Alert, Button, Group, Modal, Stack, Text } from "@mantine/core";
import { useState } from "react";

import type { Workspace } from "../../api/contracts";
import { useDeleteWorkspaceMutation } from "../../api/redpandaApi";

type RemoveWorkspaceModalProps = {
  opened: boolean;
  onClose: () => void;
  onRemoved: () => void;
  workspace: Workspace;
};

/**
 * 移除工作区只是把它从清单里摘掉：工作区目录和会话记录都不动，
 * 但这个工作区和它下面的会话从此不再出现在侧栏。
 */
export function RemoveWorkspaceModal({
  opened,
  onClose,
  onRemoved,
  workspace,
}: RemoveWorkspaceModalProps) {
  const [deleteWorkspace, { isLoading, isError, reset }] =
    useDeleteWorkspaceMutation();

  function close() {
    reset();
    onClose();
  }

  return (
    <Modal
      onClose={close}
      opened={opened}
      title="移除工作区"
      closeOnClickOutside={!isLoading}
      closeOnEscape={!isLoading}
      withCloseButton={!isLoading}
    >
      <Stack gap="sm">
        <Text size="sm">
          移除后 <Text fw={600} span>{workspace.name}</Text> 不再出现在侧栏，
          它下面的会话也不再列出。
        </Text>
        <Text c="dimmed" size="sm">
          工作区目录 {workspace.task_root} 不会被删除，会话记录也保留在本地。
        </Text>
        {isError ? (
          <Alert color="red" title="移除失败">
            未能移除工作区，请重试。
          </Alert>
        ) : null}
        <Group gap="sm" justify="flex-end">
          <Button disabled={isLoading} onClick={close} variant="default">
            取消
          </Button>
          <Button
            color="red"
            loading={isLoading}
            onClick={() => {
              void deleteWorkspace({ workspaceId: workspace.workspace_id })
                .unwrap()
                .then(() => {
                  onRemoved();
                  close();
                }, () => undefined);
            }}
          >
            移除
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
