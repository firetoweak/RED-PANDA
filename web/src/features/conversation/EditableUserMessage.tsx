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
  IconCheck,
  IconCopy,
  IconPencil,
  IconX,
} from "@tabler/icons-react";
import { useEffect, useState, type KeyboardEvent } from "react";

import { AttachmentTile, attachmentUrl } from "./AttachmentTile";
import { FileAttachmentTile } from "./FileAttachmentTile";
import { formatMessageTime } from "./messageTime";
import { composeSendContent } from "./parkDraft";

const ATTACHMENT_TOKEN = /\[(?:Image|File) #\d+\]/g;

type EditableUserMessageProps = {
  sessionId: string;
  text: string;
  occurredAt: string;
  images: string[];
  files: { attachment_id: string; name: string; size: number }[];
  disabled: boolean;
  saving: boolean;
  // 这条消息之后还跑过东西，文件就可能和这一刻对不上。
  hasLaterWork: boolean;
  onSave: (text: string, restoreFiles: boolean, artifactRefs: string[]) => Promise<void>;
};

export function EditableUserMessage({
  sessionId,
  text,
  occurredAt,
  images,
  files,
  disabled,
  saving,
  hasLaterWork,
  onSave,
}: EditableUserMessageProps) {
  const displayText = text.replace(ATTACHMENT_TOKEN, "").trim();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(displayText);
  const [retainedRefs, setRetainedRefs] = useState<string[]>([]);
  const [restoreFiles, setRestoreFiles] = useState(false);
  const [copied, setCopied] = useState(false);
  const time = formatMessageTime(occurredAt);
  const canSend = !disabled && !saving && (draft.trim() !== "" || retainedRefs.length > 0);
  const shownFiles = editing ? files.filter((file) => retainedRefs.includes(file.attachment_id)) : files;
  const fileCards = shownFiles.length === 0 ? null : (
    <Group gap={8} justify={editing ? "flex-start" : "flex-end"}>
      {shownFiles.map((file) => <FileAttachmentTile key={file.attachment_id} name={file.name} size={file.size}
        href={attachmentUrl(sessionId, file.attachment_id)}
        onRemove={editing && !saving ? () => removeAttachment(file.attachment_id) : undefined} />)}
    </Group>
  );

  useEffect(() => {
    if (!editing) {
      setDraft(displayText);
    }
  }, [editing, displayText]);

  useEffect(() => {
    if (!copied) {
      return;
    }
    const timer = window.setTimeout(() => setCopied(false), 1500);
    return () => window.clearTimeout(timer);
  }, [copied]);

  useEffect(() => {
    if (!editing) {
      return;
    }
    function cancelOnOutsidePointer() {
      if (!saving) {
        setEditing(false);
        setDraft(displayText);
      }
    }
    document.addEventListener("pointerdown", cancelOnOutsidePointer);
    return () => document.removeEventListener("pointerdown", cancelOnOutsidePointer);
  }, [editing, saving, displayText]);

  function beginEditing() {
    setDraft(displayText);
    setRetainedRefs([...images, ...files.map((file) => file.attachment_id)]);
    setRestoreFiles(false);
    setEditing(true);
  }

  function removeAttachment(attachmentId: string) {
    setRetainedRefs((current) => current.filter((ref) => ref !== attachmentId));
  }

  function close() {
    if (saving) {
      return;
    }
    setEditing(false);
    setDraft(displayText);
  }

  async function save() {
    const content = composeSendContent(draft, retainedRefs.map((ref) => ({
      kind: images.includes(ref) ? "image" : "file",
    })));
    if (!canSend) {
      return;
    }
    await onSave(content, hasLaterWork && restoreFiles, retainedRefs);
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
      void save();
    }
  }

  if (editing) {
    return (
      <Stack
        className="user-message-row user-message-editing"
        gap={8}
        onPointerDown={(event) => event.stopPropagation()}
      >
        <Paper className="user-edit" px="sm" py="sm" radius="lg" withBorder>
          <Stack gap={8}>
            {fileCards}
            {images.filter((ref) => retainedRefs.includes(ref)).length === 0 ? null : (
              <Group className="user-attachments" gap={8} justify="flex-start">
                {images.filter((ref) => retainedRefs.includes(ref)).map((attachmentId) => (
                  <AttachmentTile
                    key={attachmentId}
                    name="图片"
                    src={attachmentUrl(sessionId, attachmentId)}
                    onRemove={saving ? undefined : () => removeAttachment(attachmentId)}
                  />
                ))}
              </Group>
            )}
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
                <Tooltip label="改写并执行">
                  <ActionIcon
                    aria-label="改写并执行"
                    disabled={!canSend}
                    loading={saving}
                    onClick={() => void save()}
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
          </Stack>
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
      </Stack>
    );
  }

  return (
    <Stack className="user-message-row" align="flex-end" gap={6}>
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
      <Group className="user-message-actions" gap={4} justify="flex-end" wrap="nowrap">
        <Tooltip label={disabled ? "连接建立后可编辑" : "编辑或原文重发"}>
          <ActionIcon
            aria-label="编辑消息"
            className="user-message-action"
            disabled={disabled}
            onClick={beginEditing}
            radius="xl"
            size="sm"
            variant="subtle"
          >
            <IconPencil size={14} />
          </ActionIcon>
        </Tooltip>
        <Tooltip label={copied ? "已复制" : "复制消息"}>
          <ActionIcon
            aria-label={copied ? "已复制消息" : "复制消息"}
            className="user-message-action"
            onClick={() => {
              void navigator.clipboard.writeText(text).then(() => setCopied(true));
            }}
            radius="xl"
            size="sm"
            variant="subtle"
          >
            {copied ? <IconCheck size={14} /> : <IconCopy size={14} />}
          </ActionIcon>
        </Tooltip>
        <Text component="time" dateTime={occurredAt} c="dimmed" fz={11} ml={4} title={`发送于 ${time.full}`}>
          {time.short}
        </Text>
      </Group>
    </Stack>
  );
}
