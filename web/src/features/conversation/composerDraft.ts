import { joinRestoredText } from "./parkDraft";

type SavedDraft = { text: string; parkedText: string };

function key(sessionId: string): string {
  return `redpanda.composerDraft.${sessionId}`;
}

function read(sessionId: string): SavedDraft {
  const raw = localStorage.getItem(key(sessionId));
  if (raw === null) return { text: "", parkedText: "" };
  const value: unknown = JSON.parse(raw);
  if (value === null || typeof value !== "object" ||
      !("text" in value) || typeof value.text !== "string" ||
      !("parkedText" in value) || typeof value.parkedText !== "string") {
    throw new Error("Invalid composer draft");
  }
  return { text: value.text, parkedText: value.parkedText };
}

export function restoreComposerText(sessionId: string): string {
  const draft = read(sessionId);
  return draft.parkedText === "" ? draft.text : joinRestoredText(draft.parkedText, draft.text);
}

export function saveComposerText(sessionId: string, text: string, parkedText: string): void {
  if (text === "" && parkedText === "") {
    localStorage.removeItem(key(sessionId));
  } else {
    localStorage.setItem(key(sessionId), JSON.stringify({ text, parkedText }));
  }
}

export function clearSentComposerText(sessionId: string, field: keyof SavedDraft, sentText: string): void {
  const draft = read(sessionId);
  if (draft[field] !== sentText) return;
  draft[field] = "";
  saveComposerText(sessionId, draft.text, draft.parkedText);
}
