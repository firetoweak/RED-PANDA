import { Alert, Button, FileButton, Group, Popover, Stack, Text, UnstyledButton } from "@mantine/core";
import { IconPencil, IconRefresh, IconUpload } from "@tabler/icons-react";
import { useContext, useRef, useState } from "react";
import type { AvatarSelection } from "../../api/contracts";
import { redpandaApi, useSetAgentAvatarMutation, useUploadAgentAvatarMutation } from "../../api/redpandaApi";
import { useAppDispatch } from "../../app/hooks";
import { BotAvatar, BotAvatars } from "./ChatPresentation";

function candidates() {
  return Array.from({ length: 8 }, () => crypto.randomUUID());
}

export function BotAvatarPicker({ name, sessionId, connectionId }: {
  name: string; sessionId: string; connectionId: string | null;
}) {
  const dispatch = useAppDispatch();
  const avatars = useContext(BotAvatars);
  const resetFile = useRef<(() => void) | null>(null);
  const [opened, setOpened] = useState(false);
  const [seeds, setSeeds] = useState(candidates);
  const [error, setError] = useState<string | null>(null);
  const [setAvatar, saving] = useSetAgentAvatarMutation();
  const [uploadAvatar, uploading] = useUploadAgentAvatarMutation();
  const busy = saving.isLoading || uploading.isLoading;
  const disabled = connectionId === null || busy;

  async function save(value: string | File | null) {
    if (connectionId === null || busy) return;
    setError(null);
    try {
      const selection: AvatarSelection | null = value instanceof File
        ? await uploadAvatar({ connectionId, sessionId, file: value }).unwrap()
        : await setAvatar({ connectionId, sessionId, seed: value }).unwrap();
      dispatch(redpandaApi.util.updateQueryData("getAvatars", undefined, (current) => {
        if (selection === null) delete current[sessionId];
        else current[sessionId] = selection;
      }));
      setOpened(false);
    } catch (cause) {
      setError(typeof cause === "string" ? cause : "头像保存失败，请重试");
    } finally {
      resetFile.current?.();
    }
  }

  return <Popover opened={opened} onChange={(value) => { if (!busy) setOpened(value); }}
    position="bottom-start" width={272} trapFocus returnFocus>
    <Popover.Target>
      <UnstyledButton className="avatar-edit-trigger" aria-label={`修改 ${name} 的头像`}
        disabled={disabled} onClick={() => { setError(null); setOpened((value) => !value); }}>
        <BotAvatar name={name} sessionId={sessionId} className="agent-details-avatar" />
        <span className="avatar-edit-pencil"><IconPencil size={12} aria-hidden="true" /></span>
      </UnstyledButton>
    </Popover.Target>
    <Popover.Dropdown className="avatar-picker" aria-label={`选择 ${name} 的头像`}>
      <Stack gap="sm">
        <Group justify="space-between" gap="xs">
          <Text size="sm" fw={600}>选择头像</Text>
          <Button variant="subtle" color="gray" size="compact-xs" leftSection={<IconRefresh size={13} aria-hidden="true" />}
            disabled={disabled} onClick={() => setSeeds(candidates())}>换一批</Button>
        </Group>
        <div className="avatar-picker-options" aria-busy={busy}>
          {seeds.map((seed, index) => <UnstyledButton key={seed} className="avatar-picker-option"
            aria-label={`使用头像 ${index + 1}`} disabled={disabled}
            onClick={() => void save(seed)}>
            <BotAvatar name={name} sessionId={sessionId} avatar={{ kind: "generated", seed }} />
          </UnstyledButton>)}
        </div>
        {error === null ? null : <Alert color="orange" p="xs" role="alert">{error}</Alert>}
        <FileButton resetRef={resetFile} onChange={(file) => { if (file !== null) void save(file); }} accept="image/png,image/jpeg,image/webp,image/gif">
          {(props) => <Button {...props} variant="light" color="gray" size="xs" fullWidth
            loading={uploading.isLoading} disabled={disabled} leftSection={<IconUpload size={14} aria-hidden="true" />}>上传图片</Button>}
        </FileButton>
        <Button variant="subtle" color="gray" size="compact-xs" disabled={disabled || avatars[sessionId] === undefined}
          onClick={() => void save(null)}>恢复默认</Button>
      </Stack>
    </Popover.Dropdown>
  </Popover>;
}
