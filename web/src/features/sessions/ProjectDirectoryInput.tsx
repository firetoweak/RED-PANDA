import { Alert, Autocomplete, Button, Group, Input, Stack, Text } from "@mantine/core";
import { IconFolderOpen } from "@tabler/icons-react";
import { useId } from "react";

import type { Workspace } from "../../api/contracts";
import { useSelectWorkspaceDirectoryMutation } from "../../api/kondenApi";

export function ProjectDirectoryInput({
  label, value, onChange, workspaces, disabled, onSelectingChange,
}: {
  label: string;
  value: string;
  onChange: (path: string) => void;
  workspaces: Workspace[];
  disabled: boolean;
  onSelectingChange: (selecting: boolean) => void;
}) {
  const [selectDirectory, { isLoading, error }] = useSelectWorkspaceDirectoryMutation();
  const inputId = useId();

  async function chooseDirectory() {
    onSelectingChange(true);
    try {
      const { directory } = await selectDirectory().unwrap();
      if (directory !== null) onChange(directory.path);
    } catch {
      // The mutation error is displayed below; manual input stays available.
    } finally {
      onSelectingChange(false);
    }
  }

  return (
    <Stack gap="xs">
      <Input.Wrapper id={inputId} label={label}
        description="Git 仓库根目录，至少有一个提交；文件夹选择窗口在运行后端的电脑上打开">
        <Group gap="sm" wrap="nowrap" mt={6}>
          <Autocomplete id={inputId} aria-describedby={`${inputId}-description`}
            placeholder="选择或填写本地仓库目录" value={value}
            disabled={disabled || isLoading} onChange={onChange} clearable clearButtonProps={{ "aria-label": "清除代码目录" }}
            style={{ flex: 1, minWidth: 0 }}
            data={workspaces.map((workspace) => workspace.task_root)}
            renderOption={({ option }) => (
              <Stack gap={2} style={{ minWidth: 0 }}>
                <Text size="sm">{workspaces.find((workspace) => workspace.task_root === option.value)!.name}</Text>
                <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>{option.value}</Text>
              </Stack>
            )} />
          <Button disabled={disabled} loading={isLoading} variant="light"
            leftSection={<IconFolderOpen size={16} />} onClick={() => void chooseDirectory()}>选择文件夹</Button>
        </Group>
      </Input.Wrapper>
      {error === undefined ? null : <Alert color="red" title="选择失败">
        {typeof error === "string" ? error : "文件夹选择失败，可以直接填写目录路径。"}
      </Alert>}
    </Stack>
  );
}
