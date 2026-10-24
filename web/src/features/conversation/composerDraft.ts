type SavedDraft = { text: string };

function key(sessionId: string): string {
  return `konden.composerDraft.${sessionId}`;
}

function read(sessionId: string): SavedDraft {
  const raw = localStorage.getItem(key(sessionId));
  if (raw === null) return { text: "" };
  const value: unknown = JSON.parse(raw);
  if (value === null || typeof value !== "object" ||
      !("text" in value) || typeof value.text !== "string") {
    throw new Error("Invalid composer draft");
  }
  return { text: value.text };
}

export function restoreComposerText(sessionId: string): string {
  return read(sessionId).text;
}

export function saveComposerText(sessionId: string, text: string): void {
  if (text === "") {
    localStorage.removeItem(key(sessionId));
  } else {
    localStorage.setItem(key(sessionId), JSON.stringify({ text }));
  }
}

export function clearSentComposerText(sessionId: string, sentText: string): void {
  const draft = read(sessionId);
  if (draft.text !== sentText) return;
  saveComposerText(sessionId, "");
}
