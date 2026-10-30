import { ActionIcon, Button, Group, Text, Textarea, Tooltip } from "@mantine/core";
import { IconAt, IconArrowUp, IconPlus } from "@tabler/icons-react";
import { useContext, useEffect, useRef, useState } from "react";
import { z } from "zod";

import { useGetConversationQuery, useGetSessionModelQuery } from "../../api/kondenApi";
import { ModelSelector } from "../models/ModelSelector";
import { ChatPresentation, MessageQuote, splitMessageQuote, withMessageQuote } from "../conversation/ChatPresentation";
import { ContextUsage } from "../conversation/ContextUsage";
import { clearSentComposerText, restoreComposerText, saveComposerText } from "../conversation/composerDraft";
import { createClientId } from "../conversation/clientId";
import { useMentionCompletion } from "../conversation/MentionCompletion";
import { apiErrorDetail, useUploadAttachmentMutation } from "../../api/kondenApi";
import { AttachmentTile, attachmentUrl } from "../conversation/AttachmentTile";
import { FileAttachmentTile } from "../conversation/FileAttachmentTile";
import { composeSendContent, type ComposerAttachment } from "../conversation/composerContent";

const pendingSchema = z.object({ content: z.string(), deliveryId: z.string().min(1) }).strict();
const attachmentDraftSchema = z.array(z.object({ localId: z.string().min(1), name: z.string().min(1),
  kind: z.enum(["image", "file"]), size: z.number().nonnegative(),
  attachmentId: z.string().regex(/^(sha256:[0-9a-f]{64}|file:[0-9a-f]{32})$/) }).strict());

function releasePreview(item: ComposerAttachment) {
  if (item.previewUrl?.startsWith("blob:")) URL.revokeObjectURL(item.previewUrl);
}
export type ExternalTarget = "bot" | "codex" | "both";

export function ExternalComposer({ identity, botId, botName, connectionId, connected, posting, codexAutomatic, onSend }: {
  identity: string; botId: string; botName: string; connectionId: string | null; connected: boolean; posting: boolean;
  codexAutomatic: boolean;
  onSend: (text: string, target: ExternalTarget, deliveryId: string, replyTo: string | null, artifactRefs: string[]) => Promise<boolean>;
}) {
  const draftKey = `external:${identity}:${botId}`;
  const pendingKey = `konden.externalPending.${identity}:${botId}`;
  const attachmentsKey = `konden.externalAttachments.${identity}:${botId}`;
  const [draft, setDraft] = useState(() => restoreComposerText(draftKey));
  const [changingModel, setChangingModel] = useState(false);
  const { currentData: conversation } = useGetConversationQuery(botId);
  const { currentData: selection } = useGetSessionModelQuery(botId, { pollingInterval: 2000 });
  const limit = selection?.effective?.compact_threshold_tokens ?? selection?.selected?.compact_threshold_tokens ?? 0;
  const [uploadAttachment] = useUploadAttachmentMutation();
  const [attachments, setAttachments] = useState<ComposerAttachment[]>(() => attachmentDraftSchema
    .parse(JSON.parse(localStorage.getItem(attachmentsKey) ?? "[]"))
    .map((item) => ({ ...item, file: null, error: null, state: "done",
      previewUrl: item.kind === "image" ? attachmentUrl(botId, item.attachmentId) : null })));
  const [dragging, setDragging] = useState(false);
  const filesInput = useRef<HTMLInputElement>(null);
  const attachmentsRef = useRef(attachments);
  attachmentsRef.current = attachments;
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
  useEffect(() => () => {
    for (const item of attachmentsRef.current) releasePreview(item);
  }, []);
  const ready = attachments.filter((item) => item.state === "done" && item.attachmentId !== null);
  useEffect(() => {
    localStorage.setItem(attachmentsKey, JSON.stringify(attachments.filter((item) => item.state === "done" && item.attachmentId !== null)
      .map(({ localId, name, kind, size, attachmentId }) => ({ localId, name, kind, size, attachmentId }))));
  }, [attachments, attachmentsKey]);
  const expanded = attachments.length > 0 || quote !== null || text.length > 90 || text.includes("\n");
  const canSend = connected && !posting && !changingModel && attachments.every((item) => item.state === "done")
    && (text.trim() !== "" || ready.length > 0);

  async function upload(item: ComposerAttachment) {
    if (connectionId === null || item.file === null) return;
    setAttachments((current) => current.map((entry) => entry.localId === item.localId ? { ...entry, state: "uploading", error: null } : entry));
    try {
      const uploaded = await uploadAttachment({ connectionId, sessionId: botId, file: item.file }).unwrap();
      setAttachments((current) => current.map((entry) => entry.localId === item.localId
        ? { ...entry, state: "done", attachmentId: uploaded.attachment_id, kind: uploaded.kind, size: uploaded.size } : entry));
    } catch (cause) {
      const error = typeof cause === "object" && cause !== null && "data" in cause ? apiErrorDetail(cause.data) : undefined;
      setAttachments((current) => current.map((entry) => entry.localId === item.localId
        ? { ...entry, state: "error", error: error ?? "上传失败，请重试或移除。" } : entry));
    }
  }

  function addFiles(files: File[]) {
    if (!connected || posting) return;
    for (const file of files) {
      const kind = /^(image\/(png|jpe?g|webp|gif))$/i.test(file.type) ? "image" : "file";
      const item: ComposerAttachment = { localId: createClientId(), name: file.name, kind, file, size: file.size,
        error: null, previewUrl: kind === "image" ? URL.createObjectURL(file) : null, attachmentId: null, state: "uploading" };
      setAttachments((current) => [...current, item]);
      void upload(item);
    }
  }

  function remove(item: ComposerAttachment) {
    if (posting) return;
    releasePreview(item);
    setAttachments((current) => current.filter((entry) => entry.localId !== item.localId));
  }

  async function send() {
    if (!canSend) return;
    const saved = localStorage.getItem(pendingKey);
    const previous = saved === null ? null : pendingSchema.parse(JSON.parse(saved));
    const message = composeSendContent(text, ready);
    const refs = ready.map((item) => item.attachmentId!);
    const content = JSON.stringify([message, target, quote?.id ?? null, refs]);
    const pending = previous?.content === content ? previous : { content, deliveryId: `web-${createClientId()}` };
    localStorage.setItem(pendingKey, JSON.stringify(pending));
    if (await onSend(message, target, pending.deliveryId, quote?.id ?? null, refs)) {
      localStorage.removeItem(pendingKey);
      localStorage.removeItem(attachmentsKey);
      clearSentComposerText(draftKey, draft);
      setDraft((current) => current === draft ? "" : current);
      for (const item of ready) releasePreview(item);
      setAttachments((current) => current.filter((item) => !ready.includes(item)));
    }
  }

  return <div className={`composer group-composer external-composer${expanded ? " is-expanded" : ""}`} data-dragging={dragging || undefined}
    onDragEnter={(event) => { if ([...event.dataTransfer.types].includes("Files")) setDragging(true); }}
    onDragOver={(event) => { if ([...event.dataTransfer.types].includes("Files")) event.preventDefault(); }}
    onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragging(false); }}
    onDrop={(event) => { event.preventDefault(); setDragging(false); addFiles([...event.dataTransfer.files]); }}>
    <input type="file" multiple hidden ref={filesInput} aria-label="选择三方附件"
      onChange={(event) => { addFiles([...(event.currentTarget.files ?? [])]); event.currentTarget.value = ""; }} />
    {attachments.length === 0 ? null : <Group className="composer-attachments" gap={8} wrap="wrap">
      {attachments.map((item) => item.kind === "file" ? <FileAttachmentTile key={item.localId} name={item.name} size={item.size}
        state={item.state} error={item.error} onRemove={posting ? undefined : () => remove(item)}
        onRetry={!connected || posting ? undefined : () => void upload(item)} />
        : <div key={item.localId}><AttachmentTile name={item.name} src={item.previewUrl!} state={item.state}
          onRemove={posting ? undefined : () => remove(item)} />
          {item.state === "error" ? <Button size="compact-xs" variant="subtle" disabled={!connected || posting}
            title={item.error ?? undefined} onClick={() => void upload(item)}>重试</Button> : null}</div>)}
    </Group>}
    {quote === null ? null : <MessageQuote quote={quote} onRemove={() => { if (!posting) setDraft(text); }} />}
    {mentions.menu}
    <Textarea className="composer-input" ref={textarea} aria-label="三方会话消息" placeholder="输入 @ 选择接收方，开始三方对话…"
      variant="unstyled" autosize minRows={expanded ? 2 : 1} maxRows={7} value={text} disabled={!connected || posting}
      {...mentions.inputProps}
      onPaste={(event) => { const files = [...event.clipboardData.files]; if (files.length > 0) { event.preventDefault(); addFiles(files); } }}
      onKeyDown={(event) => {
        if (mentions.handleKeyDown(event)) return;
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault(); void send();
        }
      }} />
    <Group className="composer-meta" justify="space-between" wrap="nowrap" gap="xs">
      <Group gap={6} wrap="nowrap" className="group-composer-recipients">
        <Tooltip label="添加附件，也可拖入或粘贴文件"><ActionIcon aria-label="添加附件" variant="subtle" color="gray" radius="xl" size={27}
          disabled={!connected || posting} onClick={() => filesInput.current?.click()}><IconPlus size={17} /></ActionIcon></Tooltip>
        <ActionIcon aria-label="点名接收方" variant="subtle" color="gray" radius="xl" size={27}
          disabled={!connected || posting} onClick={mentions.insertTrigger}><IconAt size={17} /></ActionIcon>
        <Text c="dimmed" fz={12} truncate aria-live="polite">
          {addressed.size === 0 ? "默认给" : "发送给"} {recipients}{target !== "bot" && !codexAutomatic ? " · Codex 需主动收信" : ""}
        </Text>
      </Group>
      <Group className="composer-trailing" gap={8} wrap="nowrap">
        {selection === undefined ? null : <ContextUsage inputTokens={conversation?.context_input_tokens ?? null} limit={limit} />}
        <div className="composer-model"><ModelSelector sessionId={botId} connectionId={connected ? connectionId : null}
          onChanging={setChangingModel} /></div>
        <Tooltip label="发送消息"><ActionIcon className="composer-send" aria-label="发送三方消息" disabled={!canSend}
          loading={posting} loaderProps={{ color: "var(--konden-button-text)" }} radius="xl" size={32}
          onClick={() => void send()}><IconArrowUp aria-hidden="true" size={18} stroke={2} /></ActionIcon></Tooltip>
      </Group>
    </Group>
  </div>;
}
