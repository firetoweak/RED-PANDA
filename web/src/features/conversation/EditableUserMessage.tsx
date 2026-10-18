import { ActionIcon, Group, Paper, Stack, Text, Tooltip } from "@mantine/core";
import { IconCheck, IconCopy } from "@tabler/icons-react";
import { useEffect, useState } from "react";

import { AttachmentTile, attachmentUrl } from "./AttachmentTile";
import { FileAttachmentTile } from "./FileAttachmentTile";
import { formatMessageTime } from "./messageTime";

const ATTACHMENT_TOKEN = /\[(?:Image|File) #\d+\]/g;

type EditableUserMessageProps = {
  sessionId: string;
  text: string;
  occurredAt: string;
  images: string[];
  files: { attachment_id: string; name: string; size: number }[];
};

export function EditableUserMessage({
  sessionId,
  text,
  occurredAt,
  images,
  files,
}: EditableUserMessageProps) {
  const displayText = text.replace(ATTACHMENT_TOKEN, "").trim();
  const [copied, setCopied] = useState(false);
  const time = formatMessageTime(occurredAt);
  const fileCards = files.length === 0 ? null : (
    <Group gap={8} justify="flex-end">
      {files.map((file) => (
        <FileAttachmentTile
          key={file.attachment_id}
          name={file.name}
          size={file.size}
          href={attachmentUrl(sessionId, file.attachment_id)}
        />
      ))}
    </Group>
  );

  useEffect(() => {
    if (!copied) {
      return;
    }
    const timer = window.setTimeout(() => setCopied(false), 1500);
    return () => window.clearTimeout(timer);
  }, [copied]);

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
