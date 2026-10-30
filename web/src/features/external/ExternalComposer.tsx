import { ActionIcon, Group, Select, Text, Textarea, Tooltip } from "@mantine/core";
import { IconArrowUp } from "@tabler/icons-react";
import { useContext, useEffect, useRef, useState } from "react";
import { z } from "zod";

import { ChatPresentation, MessageQuote, splitMessageQuote, withMessageQuote } from "../conversation/ChatPresentation";
import { clearSentComposerText, restoreComposerText, saveComposerText } from "../conversation/composerDraft";
import { createClientId } from "../conversation/clientId";

const targetSchema = z.enum(["bot", "codex", "both"]);
const pendingSchema = z.object({ content: z.string(), deliveryId: z.string().min(1) }).strict();
export type ExternalTarget = z.infer<typeof targetSchema>;

export function ExternalComposer({ identity, botId, botName, connected, posting, codexAutomatic, onSend }: {
  identity: string; botId: string; botName: string; connected: boolean; posting: boolean;
  codexAutomatic: boolean;
  onSend: (text: string, target: ExternalTarget, deliveryId: string, replyTo: string | null) => Promise<boolean>;
}) {
  const draftKey = `external:${identity}:${botId}`;
  const targetKey = `konden.externalTarget.${identity}:${botId}`;
  const pendingKey = `konden.externalPending.${identity}:${botId}`;
  const [draft, setDraft] = useState(() => restoreComposerText(draftKey));
  const [target, setTarget] = useState(() => targetSchema.parse(localStorage.getItem(targetKey) ?? "bot"));
  const { quote: requestedQuote, onQuote } = useContext(ChatPresentation);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const { body: text, quote } = splitMessageQuote(draft);
  useEffect(() => { saveComposerText(draftKey, draft); }, [draftKey, draft]);
  useEffect(() => { localStorage.setItem(targetKey, target); }, [targetKey, target]);
  useEffect(() => {
    if (requestedQuote === null) return;
    setDraft((current) => withMessageQuote(splitMessageQuote(current).body, requestedQuote));
    onQuote(null);
    textarea.current?.focus();
  }, [requestedQuote, onQuote]);
  const canSend = connected && !posting && text.trim() !== "";

  async function send() {
    if (!canSend) return;
    const saved = localStorage.getItem(pendingKey);
    const previous = saved === null ? null : pendingSchema.parse(JSON.parse(saved));
    const content = JSON.stringify([text, target, quote?.id ?? null]);
    const pending = previous?.content === content ? previous : { content, deliveryId: `web-${createClientId()}` };
    localStorage.setItem(pendingKey, JSON.stringify(pending));
    if (await onSend(text, target, pending.deliveryId, quote?.id ?? null)) {
      localStorage.removeItem(pendingKey);
      clearSentComposerText(draftKey, draft);
      setDraft((current) => current === draft ? "" : current);
    }
  }

  return <div className="composer group-composer external-composer is-expanded">
    {quote === null ? null : <MessageQuote quote={quote} onRemove={() => { if (!posting) setDraft(text); }} />}
    <Textarea className="composer-input" ref={textarea} aria-label="三方会话消息" placeholder="在这段三方对话中发言…"
      variant="unstyled" autosize minRows={2} maxRows={7} value={text} disabled={!connected || posting}
      onChange={(event) => setDraft(withMessageQuote(event.currentTarget.value, quote))}
      onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
        event.preventDefault(); void send();
      } }} />
    <Group className="composer-meta" justify="space-between" wrap="nowrap" gap="xs">
      <Group gap="xs" wrap="nowrap" className="external-composer-recipient">
        <Select aria-label="发送给谁" value={target} allowDeselect={false} disabled={!connected || posting} size="xs"
          data={[{ value: "bot", label: `发送给 ${botName}` }, { value: "codex", label: "发送给 Codex" }, { value: "both", label: "发送给双方" }]}
          onChange={(value) => { if (value !== null) setTarget(targetSchema.parse(value)); }} />
        <Text c="dimmed" fz={11}>{target !== "bot" && !codexAutomatic ? "Codex 需主动收信" : "三方可见，接收方处理"}</Text>
      </Group>
      <Tooltip label="发送消息"><ActionIcon className="composer-send" aria-label="发送三方消息" disabled={!canSend}
        loading={posting} radius="xl" size={32} onClick={() => void send()}><IconArrowUp size={18} /></ActionIcon></Tooltip>
    </Group>
  </div>;
}
