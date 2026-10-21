import { ActionIcon, Group, Stack, Text, Tooltip } from "@mantine/core";
import { IconCheck, IconCopy } from "@tabler/icons-react";
import { useEffect, useState } from "react";

import { AttachmentTile, attachmentUrl } from "./AttachmentTile";
import { FileAttachmentTile } from "./FileAttachmentTile";
import { formatMessageTime } from "./messageTime";
import { MarkdownMessage } from "./MarkdownMessage";
import { messageReference, QuoteAction } from "./ChatPresentation";

const ATTACHMENT_TOKEN = /\[(?:Image|File) #\d+\]/g;

type UserMessageProps = {
  messageId?: string;
  sessionId: string;
  text: string;
  occurredAt: string;
  images: string[];
  files: { attachment_id: string; name: string; size: number }[];
};

export function UserMessage({
  messageId,
  sessionId,
  text,
  occurredAt,
  images,
  files,
}: UserMessageProps) {
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
    <Stack className="user-message-row user-bubble" gap={6}>
      <Group className="message-meta" gap={7} justify="flex-end"><Text fw={600} fz={12}>你</Text><Text component="time" dateTime={occurredAt} c="dimmed" fz={11} title={`发送于 ${time.full}`}>{time.short}</Text></Group>
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
        <MarkdownMessage content={displayText} streaming={false} />
      )}
      <Group className="user-message-actions" gap={4} justify="flex-end" wrap="nowrap">
        {messageId === undefined ? null : <QuoteAction message={messageReference(messageId, "你", text)} />}
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
      </Group>
    </Stack>
  );
}
