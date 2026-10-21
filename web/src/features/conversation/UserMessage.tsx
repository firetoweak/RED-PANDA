import { Group, Stack, Text } from "@mantine/core";

import { AttachmentTile, attachmentUrl } from "./AttachmentTile";
import { FileAttachmentTile } from "./FileAttachmentTile";
import { formatMessageTime } from "./messageTime";
import { MarkdownMessage } from "./MarkdownMessage";
import { MessageMenu } from "./ChatPresentation";

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

  return (
    <div className="user-message-content">
    <MessageMenu id={messageId} author="你" text={text}>
    <Stack className="user-message-row user-bubble" gap={6} tabIndex={0}>
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
    </Stack>
    </MessageMenu>
    <Text className="message-time" component="time" dateTime={occurredAt} c="dimmed" fz={11} title={`发送于 ${time.full}`}>{time.short} · 已发送</Text>
    </div>
  );
}
