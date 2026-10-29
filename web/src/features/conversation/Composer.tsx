import {
  ActionIcon,
  Button,
  Group,
  Paper,
  Text,
  Textarea,
  Tooltip,
} from "@mantine/core";
import {
  IconArrowUp,
  IconPlayerStopFilled,
  IconPlus,
} from "@tabler/icons-react";
import {
  useEffect,
  useContext,
  useRef,
  useState,
  type ClipboardEvent,
  type DragEvent,
  type FormEvent,
  type KeyboardEvent,
} from "react";

import {
  useGetSessionModelQuery,
  useGetModelSettingsQuery,
  useGetWorkspacesQuery,
  useSelectLocalFileMutation,
  useUploadAttachmentMutation,
} from "../../api/kondenApi";
import { ModelSelector } from "../models/ModelSelector";
import { AttachmentTile } from "./AttachmentTile";
import { FileAttachmentTile } from "./FileAttachmentTile";
import { createClientId } from "./clientId";
import { clearSentComposerText, restoreComposerText, saveComposerText } from "./composerDraft";
import { ChatPresentation, MessageQuote, splitMessageQuote, withMessageQuote } from "./ChatPresentation";
import { useMentionCompletion } from "./MentionCompletion";
import {
  composeSendContent,
  type ComposerDraft,
  type ComposerAttachment,
} from "./composerContent";

const ACCEPTED_IMAGE_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/webp",
  "image/gif",
]);

type ComposerProps = {
  sessionId: string;
  workspaceId: string | null;
  connectionId: string | null;
  disabled: boolean;
  sending: boolean;
  running: boolean;
  canStop?: boolean;
  stopBusy: boolean;
  onSend: (text: string, artifactRefs: string[]) => Promise<void>;
  onStop: () => void;
  inputTokens: number | null;
};

export function Composer({
  sessionId,
  workspaceId,
  connectionId,
  disabled,
  sending,
  running,
  canStop = running,
  stopBusy,
  onSend,
  onStop,
  inputTokens,
}: ComposerProps) {
  const [text, setText] = useState(() => restoreComposerText(sessionId));
  const { names, quote: requestedQuote, onQuote } = useContext(ChatPresentation);
  const composed = splitMessageQuote(text);
  useEffect(() => {
    if (requestedQuote === null) return;
    setText((current) => withMessageQuote(splitMessageQuote(current).body, requestedQuote));
    onQuote(null);
    textareaRef.current?.focus();
  }, [requestedQuote, onQuote]);
  const [pending, setPending] = useState<ComposerAttachment[]>([]);
  const [dragging, setDragging] = useState(false);
  const [changingModelFor, setChangingModelFor] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const mentions = useMentionCompletion({ text: composed.body, names, textarea: textareaRef,
    onChange: (body) => setText(withMessageQuote(body, composed.quote)) });
  const onSendRef = useRef(onSend);
  onSendRef.current = onSend;
  const { data: selection, error: selectionError } = useGetSessionModelQuery(sessionId, { pollingInterval: 2000 });
  const { data: settings, error: settingsError } = useGetModelSettingsQuery(undefined, { pollingInterval: 5000 });
  const { data: workspaces = [] } = useGetWorkspacesQuery();
  const workspacePath = workspaces.find(
    (workspace) => workspace.workspace_id === workspaceId,
  )?.task_root;
  const [uploadAttachment] = useUploadAttachmentMutation();
  const [selectLocalFile] = useSelectLocalFileMutation();
  const pendingRef = useRef(pending);
  pendingRef.current = pending;
  const limit = selection?.effective?.compact_threshold_tokens ?? selection?.selected?.compact_threshold_tokens ?? 0;
  const usagePercent = inputTokens !== null && limit > 0 ? Math.round(inputTokens / limit * 100) : null;
  const usageTitle = usagePercent !== null ? `${usagePercent}% 已用`
    : inputTokens === null ? "暂无本窗口用量" : "上下文用量";
  const usageTokens = `${inputTokens === null ? "—" : formatTokens(inputTokens)} / ${limit > 0 ? formatTokens(limit) : "—"} tokens`;
  const modelReady = selection?.selected != null && settings?.providers.some(
    (item) => item.provider === selection.selected?.model.split("/")[0] && item.configured,
  );
  const uploading = pending.some((item) => item.state === "uploading");
  const ready = pending.filter(
    (item) => item.state === "done" && item.attachmentId !== null,
  );
  const busy = disabled || sending || uploading || stopBusy || changingModelFor === sessionId;
  const canSend =
    !busy &&
    !selectionError && !settingsError &&
    modelReady &&
    !pending.some((item) => item.state === "error") &&
    (composed.body.trim() !== "" || ready.length > 0);

  useEffect(() => {
    saveComposerText(sessionId, text);
  }, [sessionId, text]);

  useEffect(() => {
    return () => {
      for (const item of pendingRef.current) {
        releasePreview(item);
      }
    };
  }, []);

  async function sendDraft(draft: ComposerDraft) {
    const done = draft.pending.filter(
      (item) => item.state === "done" && item.attachmentId !== null,
    );
    const content = composeSendContent(draft.text, done);
    const artifactRefs = done.map((item) => item.attachmentId as string);
    await onSendRef.current(content, artifactRefs);
    clearSentComposerText(sessionId, draft.text);
    for (const item of draft.pending) {
      releasePreview(item);
    }
  }

  async function submit(event?: FormEvent) {
    event?.preventDefault();
    if (!canSend) {
      return;
    }
    const draft: ComposerDraft = { text, pending };
    setPending([]);
    try {
      await sendDraft(draft);
      setText((current) => current === draft.text ? "" : current);
    } catch {
      setPending(draft.pending);
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (mentions.handleKeyDown(event)) return;
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  }

  async function addAttachment(file: File) {
    if (connectionId === null || disabled) {
      return;
    }
    const localId = createClientId();
    const kind = ACCEPTED_IMAGE_TYPES.has(normalizeMime(file.type)) ? "image" : "file";
    const previewUrl = kind === "image" ? URL.createObjectURL(file) : null;
    setPending((current) => [
      ...current,
      {
        localId,
        name: file.name,
        kind,
        file,
        size: file.size,
        error: null,
        previewUrl,
        attachmentId: null,
        state: "uploading",
      },
    ]);
    await upload(localId, file);
  }

  async function addLocalPath(retryId?: string) {
    if (connectionId === null || disabled) {
      return;
    }
    const localId = retryId ?? createClientId();
    setPending((current) => retryId === undefined ? [
      ...current,
      {
        localId, name: "本机文件", kind: "file", file: null, size: 0,
        error: null, previewUrl: null, attachmentId: null, state: "uploading",
      },
    ] : current.map((item) => item.localId === localId
      ? { ...item, state: "uploading", error: null } : item));
    try {
      const selection = await selectLocalFile({ connectionId, sessionId }).unwrap();
      if (selection.file === null) {
        setPending((current) => current.filter((item) => item.localId !== localId));
        return;
      }
      const chosen = selection.file;
      setPending((current) =>
        current.map((item) =>
          item.localId === localId
            ? {
                ...item,
                attachmentId: chosen.attachment_id,
                name: chosen.name,
                size: chosen.size,
                kind: "file",
                state: "done",
              }
            : item,
        ),
      );
    } catch (error) {
      setPending((current) =>
        current.map((item) =>
          item.localId === localId ? { ...item, state: "error", error: uploadError(error) } : item,
        ),
      );
    }
  }

  async function upload(localId: string, file: File) {
    if (connectionId === null || disabled) {
      return;
    }
    setPending((current) => current.map((item) =>
      item.localId === localId ? { ...item, state: "uploading", error: null } : item,
    ));
    try {
      const uploaded = await uploadAttachment({
        connectionId,
        sessionId,
        file,
      }).unwrap();
      setPending((current) =>
        current.map((item) =>
          item.localId === localId
            ? {
                ...item,
                attachmentId: uploaded.attachment_id,
                kind: uploaded.kind,
                size: uploaded.size,
                state: "done",
              }
            : item,
        ),
      );
    } catch (error) {
      setPending((current) =>
        current.map((item) =>
          item.localId === localId ? { ...item, state: "error", error: uploadError(error) } : item,
        ),
      );
    }
  }

  function retryAttachment(item: ComposerAttachment) {
    if (item.file === null) {
      void addLocalPath(item.localId);
      return;
    }
    if (item.file !== null) {
      void upload(item.localId, item.file);
    }
  }

  function addFiles(files: File[]) {
    void Promise.all(files.map((file) => addAttachment(file)));
  }

  function removeAttachment(item: ComposerAttachment) {
    releasePreview(item);
    setPending((current) => current.filter((entry) => entry.localId !== item.localId));
  }

  function onPaste(event: ClipboardEvent<HTMLTextAreaElement>) {
    const files = [...(event.clipboardData?.files ?? [])];
    if (files.length === 0) {
      return;
    }
    event.preventDefault();
    addFiles(files);
  }

  function onDrop(event: DragEvent<HTMLFormElement>) {
    const files = [...event.dataTransfer.files];
    setDragging(false);
    if (files.length === 0) {
      return;
    }
    event.preventDefault();
    addFiles(files);
  }

  return (
    <>
    <Paper
      component="form"
      className="composer private-composer"
      data-dragging={dragging || undefined}
      onSubmit={(event: FormEvent) => void submit(event)}
      onDragEnter={(event) => {
        if ([...event.dataTransfer.types].includes("Files")) {
          setDragging(true);
        }
      }}
      onDragOver={(event) => {
        if ([...event.dataTransfer.types].includes("Files")) {
          event.preventDefault();
        }
      }}
      onDragLeave={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node)) {
          setDragging(false);
        }
      }}
      onDrop={onDrop}
    >
      <input
        hidden
        multiple
        onChange={(event) => {
          const files = [...(event.currentTarget.files ?? [])];
          event.currentTarget.value = "";
          addFiles(files);
        }}
        type="file"
      />
      {pending.length === 0 ? null : (
        <Group className="composer-attachments" gap={8} wrap="wrap">
          {pending.map((item) => item.kind === "file" ? (
            <FileAttachmentTile key={item.localId} name={item.name} size={item.size}
              state={item.state} error={item.error}
              onRetry={disabled ? undefined : () => retryAttachment(item)}
              onRemove={() => removeAttachment(item)} />
          ) : (
            <div key={item.localId}>
              <AttachmentTile
                name={item.name}
                onRemove={() => removeAttachment(item)}
                src={item.previewUrl!}
                state={item.state}
              />
              {item.state === "error" && item.file !== null ? <Button disabled={disabled} size="compact-xs" variant="subtle"
                title={item.error ?? undefined} onClick={() => retryAttachment(item)}>重试</Button> : null}
            </div>
          ))}
        </Group>
      )}
      {composed.quote === null ? null : <MessageQuote quote={composed.quote} onRemove={() => setText(composed.body)} />}
      {mentions.menu}
      <Textarea
        aria-label="消息"
        autosize
        className="composer-input"
        ref={textareaRef}
        value={composed.body}
        {...mentions.inputProps}
        onKeyDown={onKeyDown}
        onPaste={onPaste}
        placeholder={disabled ? "正在连接…" : "输入消息，@ 选择 Bot…"}
        minRows={1}
        maxRows={7}
        variant="unstyled"
        disabled={disabled}
      />
      <Group className="composer-meta" gap={8} wrap="nowrap" align="center">
          <Tooltip label="选择窗口在运行后端的电脑上打开，也可拖入或粘贴文件">
            <ActionIcon
              aria-label="Add attachment"
              color="gray"
              disabled={busy || connectionId === null}
              onClick={() => void addLocalPath()}
              radius="xl"
              size={28}
              type="button"
              variant="subtle"
            >
              <IconPlus size={16} />
            </ActionIcon>
          </Tooltip>
        {workspacePath === undefined ? null : (
          <Text className="composer-workspace" c="dimmed" ff="monospace" fz={11}
            title={workspacePath} truncate>
            {workspacePath}
          </Text>
        )}
        <Group className="composer-trailing" gap={8} wrap="nowrap">
          {selection === undefined ? null : (
            <Tooltip radius="md" label={<div>
              <Text size="sm" ta="center">
                {usagePercent === null ? usageTitle : (
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
                    <span>{usagePercent}%</span><span>已用</span>
                  </span>
                )}
              </Text>
              <Text size="sm" ta="center" style={{ opacity: 0.65 }}>{usageTokens}</Text>
            </div>}>
              <span role="img" aria-label={usageTitle + " · " + usageTokens}
                style={{ display: "inline-flex", padding: 6 }}>
                <ContextRing used={inputTokens} limit={limit} />
              </span>
            </Tooltip>
          )}
          <div className="composer-model"><ModelSelector sessionId={sessionId} connectionId={connectionId}
            onChanging={(changing) => setChangingModelFor((current) => changing ? sessionId : current === sessionId ? null : current)} /></div>
          <Group gap={6} wrap="nowrap">
            {canStop ? (
              <Tooltip label="停止">
                <ActionIcon
                  aria-label="停止"
                  color="gray"
                  disabled={stopBusy || connectionId === null}
                  loading={stopBusy}
                  onClick={onStop}
                  radius="xl"
                  size={32}
                  type="button"
                  variant="light"
                >
                  <IconPlayerStopFilled size={16} />
                </ActionIcon>
              </Tooltip>
            ) : null}
            <Tooltip label="发送">
              <ActionIcon
                className="composer-send"
                aria-label="发送"
                disabled={!canSend}
                loading={sending}
                loaderProps={{ color: "var(--konden-button-text)" }}
                radius="xl"
                size={32}
                type="submit"
              >
                <IconArrowUp aria-hidden="true" size={18} stroke={2} />
              </ActionIcon>
            </Tooltip>
          </Group>
        </Group>
      </Group>
    </Paper>
    </>
  );
}

function ContextRing({ used, limit }: { used: number | null; limit: number }) {
  const ratio = used !== null && limit > 0 ? Math.min(used / limit, 1) : 0;
  const radius = 5;
  const circumference = 2 * Math.PI * radius;
  return (
    <svg
      aria-hidden
      className="context-ring"
      height="14"
      viewBox="0 0 14 14"
      width="14"
    >
      <circle
        cx="7"
        cy="7"
        fill="none"
        r={radius}
        stroke="var(--konden-ring-track)"
        strokeWidth="2"
      />
      <circle
        cx="7"
        cy="7"
        fill="none"
        r={radius}
        stroke="var(--mantine-color-ember-4)"
        strokeDasharray={circumference}
        strokeDashoffset={circumference * (1 - ratio)}
        strokeLinecap="round"
        strokeWidth="2"
        transform="rotate(-90 7 7)"
      />
    </svg>
  );
}

function formatTokens(tokens: number) {
  if (tokens < 1000) {
    return String(tokens);
  }
  if (tokens % 1000 === 0) {
    return `${tokens / 1000}K`;
  }
  return `${(tokens / 1000).toFixed(1)}K`;
}

function normalizeMime(type: string) {
  const mime = type.split(";", 1)[0].trim().toLowerCase();
  return mime === "image/jpg" ? "image/jpeg" : mime;
}

function releasePreview(item: ComposerAttachment) {
  if (item.previewUrl !== null) URL.revokeObjectURL(item.previewUrl);
}

function uploadError(error: unknown): string {
  if (typeof error === "string" && error !== "") {
    return error;
  }
  if (typeof error === "object" && error !== null && "data" in error) {
    const data = error.data;
    if (typeof data === "object" && data !== null && "detail" in data && typeof data.detail === "string") return data.detail;
  }
  return "上传失败，请重试或移除";
}
