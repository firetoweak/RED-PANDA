export type ComposerAttachment = {
  localId: string;
  name: string;
  previewUrl: string | null;
  kind: "image" | "file";
  file: File | null;
  size: number;
  error: string | null;
  attachmentId: string | null;
  state: "uploading" | "done" | "error";
};

export type ComposerDraft = {
  text: string;
  pending: ComposerAttachment[];
};

export function composeSendContent(text: string, attachments: Pick<ComposerAttachment, "kind">[]): string {
  let images = 0;
  let files = 0;
  const tokens = attachments.map((item) => item.kind === "image"
    ? `[Image #${++images}]` : `[File #${++files}]`);
  return [text.trim(), ...tokens].filter(Boolean).join(" ");
}
