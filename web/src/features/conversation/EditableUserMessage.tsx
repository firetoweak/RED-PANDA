import {
  ActionIcon,
  Checkbox,
  Group,
  Paper,
  Stack,
  Text,
  Textarea,
  Tooltip,
} from "@mantine/core";
import {
  IconArrowUp,
  IconGitBranch,
  IconPencil,
  IconX,
} from "@tabler/icons-react";
import { useEffect, useState, type KeyboardEvent } from "react";

import { AttachmentTile, attachmentUrl } from "./AttachmentTile";
import { FileAttachmentTile } from "./FileAttachmentTile";

const ATTACHMENT_TOKEN = /\[(?:Image|File) #\d+\]/g;

type EditableUserMessageProps = {
  sessionId: string;
  text: string;
  images: string[];
  files: { attachment_id: string; name: string; size: number }[];
  disabled: boolean;
  saving: boolean;
  // 这条消息之后还跑过东西，文件就可能和这一刻对不上。
  hasLaterWork: boolean;
  onSave: (
    text: string,
    listed: boolean,
    restoreFiles: boolean,
  ) => Promise<void>;
};

export function EditableUserMessage({
  sessionId,
  text,
  images,
  files,
  disabled,
  saving,
  hasLaterWork,
  onSave,
}: EditableUserMessageProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(text);
  const [restoreFiles, setRestoreFiles] = useState(false);
  const displayText = text.replace(ATTACHMENT_TOKEN, "").trim();
  const canSend = !disabled && !saving && draft.trim() !== "";
  const fileCards = files.length === 0 ? null : (
    <Group gap={8} justify="flex-end">
      {files.map((file) => <FileAttachmentTile key={file.attachment_id} name={file.name} size={file.size} href={attachmentUrl(sessionId, file.attachment_id)} />)}
    </Group>
  );

  useEffect(() => {
    if (!editing) {
      setDraft(text);
    }
  }, [editing, text]);

  function close() {
    if (saving) {
      return;
    }
    setEditing(false);
    setDraft(text);
  }

  async function save(listed: boolean) {
    const content = draft.trim();
    if (!canSend) {
      return;
    }
    await onSave(content, listed, hasLaterWork && restoreFiles);
    setEditing(false);
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.nativeEvent.isComposing) {
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      close();
      return;
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void save(false);
    }
  }

  if (editing) {
    return (
      <Stack className="user-message-row user-message-editing" gap={8}>
        {fileCards}
        {images.length === 0 ? null : (
          <Group className="user-attachments" gap={8} justify="flex-end">
            {images.map((attachmentId) => (
              <AttachmentTile
                key={attachmentId}
                name="图片"
                src={attachmentUrl(sessionId, attachmentId)}
              />
            ))}
          </Group>
        )}
        <Paper className="user-edit" px="sm" py={6} radius="lg" withBorder>
          <Group align="flex-end" gap={8} wrap="nowrap">
            <Textarea
              aria-label="编辑消息"
              autosize
              autoFocus
              className="user-edit-input"
              disabled={saving}
              maxRows={12}
              minRows={1}
              onChange={(event) => setDraft(event.currentTarget.value)}
              onKeyDown={onKeyDown}
              value={draft}
              variant="unstyled"
            />
            <Group gap={6} wrap="nowrap">
              <Tooltip label="取消">
                <ActionIcon
                  aria-label="取消编辑"
                  disabled={saving}
                  onClick={close}
                  radius="xl"
                  size={32}
                  type="button"
                  variant="subtle"
                >
                  <IconX size={16} />
                </ActionIcon>
              </Tooltip>
              <Tooltip label="作为新分支执行">
                <ActionIcon
                  aria-label="作为新分支执行"
                  disabled={!canSend}
                  loading={saving}
                  onClick={() => void save(true)}
                  radius="xl"
                  size={32}
                  type="button"
                  variant="subtle"
                >
                  <IconGitBranch size={16} />
                </ActionIcon>
              </Tooltip>
              <Tooltip label="改写并执行">
                <ActionIcon
                  aria-label="改写并执行"
                  disabled={!canSend}
                  loading={saving}
                  onClick={() => void save(false)}
                  radius="xl"
                  size={32}
                  type="button"
                  variant="filled"
                >
                  <IconArrowUp size={16} stroke={2.2} />
                </ActionIcon>
              </Tooltip>
            </Group>
          </Group>
        </Paper>
        {hasLaterWork ? (
          <Checkbox
            checked={restoreFiles}
            disabled={saving}
            label="同时把工作区文件退回这条消息之前"
            onChange={(event) => setRestoreFiles(event.currentTarget.checked)}
            size="xs"
          />
        ) : null}
        <Text c="dimmed" size="xs" ta="right">
          改写就地接着这条会话走；作为新分支会在侧栏多出一条，原会话留在原处。
        </Text>
      </Stack>
    );
  }

  return (
    <Group className="user-message-row" align="flex-start" gap={6} wrap="nowrap">
      <Tooltip label={disabled ? "连接建立后可编辑" : "编辑或原文重发"}>
        <ActionIcon
          aria-label="编辑消息"
          className="edit-message-button"
          disabled={disabled}
          onClick={() => setEditing(true)}
          radius="xl"
          size="sm"
          variant="subtle"
        >
          <IconPencil size={14} />
        </ActionIcon>
      </Tooltip>
      <Stack align="flex-end" gap={8}>
        {fileCards}
        {images.length === 0 ? null : (
          <Group className="user-attachments" gap={8} justify="flex-end">
            {images.map((attachmentId) => (
              <AttachmentTile
                key={attachmentId}
                large={images.length === 1 && displayText === ""}
                name="图片"
                src={attachmentUrl(sessionId, attachmentId)}
              />
            ))}
          </Group>
        )}
        {displayText === "" ? null : (
          <Paper className="user-bubble" px="md" py="sm" radius="xl">
            <Text className="message-text" lh={1.6} size="sm">
              {displayText}
            </Text>
          </Paper>
        )}
      </Stack>
    </Group>
  );
}
