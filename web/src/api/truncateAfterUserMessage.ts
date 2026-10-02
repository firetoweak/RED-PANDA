import type { ConversationView } from "./contracts";

export function truncateAfterUserMessage(
  conversation: ConversationView,
  messageId: string,
  text: string,
  artifactRefs: string[],
): ConversationView {
  const index = conversation.items.findIndex(
    (item) => item.kind === "user" && item.message_id === messageId,
  );
  if (index < 0) {
    return conversation;
  }
  const target = conversation.items[index];
  if (target.kind !== "user") {
    return conversation;
  }
  return {
    ...conversation,
    items: [...conversation.items.slice(0, index), {
      ...target, text,
      images: target.images.filter((ref) => artifactRefs.includes(ref)),
      files: target.files.filter((file) => artifactRefs.includes(file.attachment_id)),
    }],
  };
}
