export type ComposerAttachment = {
  localId: string;
  name: string;
  previewUrl: string | null;
  kind: "image" | "file";
  file: File | null;
  size: number;
  sourcePath: string | null;
  error: string | null;
  attachmentId: string | null;
  state: "uploading" | "done" | "error";
};

export type ComposerDraft = {
  text: string;
  pending: ComposerAttachment[];
};

export function restoreParkedDraft(
  parked: ComposerDraft,
  current: ComposerDraft,
): ComposerDraft {
  return {
    text: joinRestoredText(parked.text, current.text),
    pending: [...parked.pending, ...current.pending],
  };
}

export function joinRestoredText(parkedText: string, currentText: string): string {
  if (currentText.trim() === "") {
    return parkedText;
  }
  if (parkedText.trim() === "") {
    return currentText;
  }
  return `${parkedText.replace(/\s+$/u, "")}\n${currentText.replace(/^\s+/u, "")}`;
}

export function composeSendContent(text: string, attachments: Pick<ComposerAttachment, "kind">[]): string {
  let images = 0;
  let files = 0;
  const tokens = attachments.map((item) => item.kind === "image"
    ? `[Image #${++images}]` : `[File #${++files}]`);
  return [text.trim(), ...tokens].filter(Boolean).join(" ");
}

export function parkedPreviewText(draft: ComposerDraft): string {
  const text = draft.text.trim();
  if (text !== "") {
    return text;
  }
  return draft.pending.length > 0 ? `附件（${draft.pending.length}）` : "";
}
