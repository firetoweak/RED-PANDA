import { ActionIcon, Group, Text, Textarea, Tooltip } from "@mantine/core";
import { IconAt, IconArrowUp } from "@tabler/icons-react";
import { useContext, useEffect, useRef, useState } from "react";
import { z } from "zod";

import { ChatPresentation, MessageQuote, splitMessageQuote, withMessageQuote } from "../conversation/ChatPresentation";
import { clearSentComposerText, restoreComposerText, saveComposerText } from "../conversation/composerDraft";
import { createClientId } from "../conversation/clientId";
import { useMentionCompletion } from "../conversation/MentionCompletion";

const pendingSchema = z.object({ content: z.string(), deliveryId: z.string().min(1) }).strict();
export type ExternalTarget = "bot" | "codex" | "both";

export function ExternalComposer({ identity, botId, botName, connected, posting, codexAutomatic, onSend }: {
  identity: string; botId: string; botName: string; connected: boolean; posting: boolean;
  codexAutomatic: boolean;
  onSend: (text: string, target: ExternalTarget, deliveryId: string, replyTo: string | null) => Promise<boolean>;
}) {
  const draftKey = `external:${identity}:${botId}`;
  const pendingKey = `konden.externalPending.${identity}:${botId}`;
  const [draft, setDraft] = useState(() => restoreComposerText(draftKey));
  const { quote: requestedQuote, onQuote } = useContext(ChatPresentation);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const { body: text, quote } = splitMessageQuote(draft);
  const setText = (body: string) => setDraft(withMessageQuote(body, quote));
  const botMention = ["codex", "all"].includes(botName.toLocaleLowerCase()) ? `${botName}（Bot）` : botName;
  const mentions = useMentionCompletion({ text, onChange: setText, names: [botMention, "Codex"], textarea,
    includeAll: true, heading: "选择接收方", allLabel: "双方" });
  const names = [botMention, "Codex", "all"].sort((a, b) => b.length - a.length)
    .map((name) => name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|");
  const addressed = new Set([...text.matchAll(new RegExp(`(^|[^A-Za-z0-9._%+-])@(${names})(?=$|[\\s，。！？、,:;!?）)])`, "giu"))]
    .map((match) => match[2].toLocaleLowerCase()));
  const target: ExternalTarget = addressed.has("all") || addressed.has("codex") && addressed.has(botMention.toLocaleLowerCase())
    ? "both" : addressed.has("codex") ? "codex" : "bot";
  const recipients = target === "both" ? `@${botMention}、@Codex` : target === "codex" ? "@Codex" : `@${botMention}`;
  useEffect(() => { saveComposerText(draftKey, draft); }, [draftKey, draft]);
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
    {mentions.menu}
    <Textarea className="composer-input" ref={textarea} aria-label="三方会话消息" placeholder="输入 @ 选择接收方，开始三方对话…"
      variant="unstyled" autosize minRows={2} maxRows={7} value={text} disabled={!connected || posting}
      {...mentions.inputProps}
      onKeyDown={(event) => {
        if (mentions.handleKeyDown(event)) return;
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault(); void send();
        }
      }} />
    <Group className="composer-meta" justify="space-between" wrap="nowrap" gap="xs">
      <Group gap={6} wrap="nowrap" className="group-composer-recipients">
        <ActionIcon aria-label="点名接收方" variant="subtle" color="gray" radius="xl" size={27}
          disabled={!connected || posting} onClick={mentions.insertTrigger}><IconAt size={17} /></ActionIcon>
        <Text c="dimmed" fz={12} truncate aria-live="polite">
          {addressed.size === 0 ? "默认给" : "发送给"} {recipients}{target !== "bot" && !codexAutomatic ? " · Codex 需主动收信" : ""}
        </Text>
      </Group>
      <Tooltip label="发送消息"><ActionIcon className="composer-send" aria-label="发送三方消息" disabled={!canSend}
        loading={posting} radius="xl" size={32} onClick={() => void send()}><IconArrowUp size={18} /></ActionIcon></Tooltip>
    </Group>
  </div>;
}
