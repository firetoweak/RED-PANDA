import { Alert, Button, Group, Modal, Stack, TextInput } from "@mantine/core";
import { IconFolderOpen } from "@tabler/icons-react";
import { useState } from "react";

import {
  useCreateWorkspaceMutation,
  useSelectWorkspaceDirectoryMutation,
} from "../../api/helpermeApi";

type CreateWorkspaceModalProps = {
  opened: boolean;
  onClose: () => void;
};

/**
 * 工作区只能由用户显式建：一个名字 + 一个已存在的目录。
 * 目录不存在、或已被别的工作区占用时，后端会拒绝（这里只说清两种原因）。
 */
export function CreateWorkspaceModal({
  opened,
  onClose,
}: CreateWorkspaceModalProps) {
  const [name, setName] = useState("");
  const [taskRoot, setTaskRoot] = useState("");
  const [createWorkspace, { isLoading, isError, reset }] =
    useCreateWorkspaceMutation();
  const [
    selectDirectory,
    { isLoading: isSelecting, error: selectionError, reset: resetSelection },
  ] = useSelectWorkspaceDirectoryMutation();
  const canSubmit = name.trim() !== "" && taskRoot.trim() !== "";

  function close() {
    setName("");
    setTaskRoot("");
    reset();
    resetSelection();
    onClose();
  }

  return (
    <Modal
      onClose={close}
      opened={opened}
      title="新建工作区"
      closeOnClickOutside={!isSelecting}
      closeOnEscape={!isSelecting}
      withCloseButton={!isSelecting}
    >
      <Stack gap="sm">
        <TextInput
          disabled={isSelecting || isLoading}
          label="名称"
          onChange={(event) => setName(event.currentTarget.value)}
          placeholder="ai_charter"
          value={name}
        />
        <Group align="flex-end" gap="sm" wrap="nowrap">
          <TextInput
            description="选择本机已有的文件夹，也可以直接填写路径"
            disabled={isSelecting || isLoading}
            label="目录"
            onChange={(event) => setTaskRoot(event.currentTarget.value)}
            placeholder="请选择工作区文件夹"
            style={{ flex: 1, minWidth: 0 }}
            value={taskRoot}
          />
          <Button
            disabled={isLoading}
            leftSection={<IconFolderOpen size={16} />}
            loading={isSelecting}
            variant="light"
            onClick={() => {
              void selectDirectory()
                .unwrap()
                .then(({ directory }) => {
                  if (directory !== null) {
                    setTaskRoot(directory.path);
                    setName((current) =>
                      current.trim() === "" ? directory.name : current,
                    );
                    reset();
                  }
                }, () => undefined);
            }}
          >
            选择文件夹
          </Button>
        </Group>
        {selectionError !== undefined ? (
          <Alert color="red" title="选择失败">
            {typeof selectionError === "string"
              ? selectionError
              : "文件夹选择失败，请查看服务端错误。"}
          </Alert>
        ) : null}
        {isError ? (
          <Alert color="red" title="创建失败">
            目录不存在，或者已经被另一个工作区占用。
          </Alert>
        ) : null}
        <Button
          disabled={!canSubmit || isSelecting}
          loading={isLoading}
          onClick={() => {
            void createWorkspace({
              name: name.trim(),
              taskRoot: taskRoot.trim(),
              fullAccess: false,
            })
              .unwrap()
              .then(close, () => undefined);
          }}
        >
          创建
        </Button>
      </Stack>
    </Modal>
  );
}
