import {
  ActionIcon,
  Button,
  Group,
  Paper,
  Switch,
  Text,
  Textarea,
  Tooltip,
} from "@mantine/core";
import {
  IconArrowUp,
  IconPlayerPauseFilled,
  IconPlayerPlayFilled,
  IconPlus,
  IconTrash,
} from "@tabler/icons-react";
import {
  useEffect,
  useRef,
  useState,
  type ClipboardEvent,
  type DragEvent,
  type FormEvent,
  type KeyboardEvent,
} from "react";

import {
  useAttachLocalFileMutation,
  useGetSessionModelQuery,
  useGetModelSettingsQuery,
  useGetWorkspacesQuery,
  useSelectLocalFileMutation,
  useUploadAttachmentMutation,
} from "../../api/helpermeApi";
import { ModelSelector } from "../models/ModelSelector";
import { AttachmentTile } from "./AttachmentTile";
import { FileAttachmentTile } from "./FileAttachmentTile";
import { createClientId } from "./clientId";
import {
  composeSendContent,
  parkedPreviewText,
  restoreParkedDraft,
  type ComposerDraft,
  type ComposerAttachment,
} from "./parkDraft";

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
  paused: boolean;
  shouldWake: boolean;
  pauseBusy: boolean;
  retryBusy: boolean;
  autoAuthorize: boolean;
  autoAuthorizeBusy: boolean;
  onToggleAutoAuthorize: (enabled: boolean) => void;
  onSend: (text: string, artifactRefs: string[]) => Promise<void>;
  onSetPaused: (paused: boolean) => void;
  onRetry: () => void;
  inputTokens: number | null;
};

export function Composer({
  sessionId,
  workspaceId,
  connectionId,
  disabled,
  sending,
  running,
  paused,
  shouldWake,
  pauseBusy,
  retryBusy,
  autoAuthorize,
  autoAuthorizeBusy,
  onToggleAutoAuthorize,
  onSend,
  onSetPaused,
  onRetry,
  inputTokens,
}: ComposerProps) {
  const [text, setText] = useState("");
  const [pending, setPending] = useState<ComposerAttachment[]>([]);
  const [parked, setParked] = useState<ComposerDraft | null>(null);
  const [dragging, setDragging] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const parkedRef = useRef<ComposerDraft | null>(null);
  const flushingRef = useRef(false);
  const wasRunningRef = useRef(running);
  const onSendRef = useRef(onSend);
  parkedRef.current = parked;
  onSendRef.current = onSend;
  const { data: selection, error: selectionError } = useGetSessionModelQuery(sessionId, { pollingInterval: 2000 });
  const { data: settings, error: settingsError } = useGetModelSettingsQuery(undefined, { pollingInterval: 5000 });
  const { data: workspaces = [] } = useGetWorkspacesQuery();
  const workspacePath = workspaces.find(
    (workspace) => workspace.workspace_id === workspaceId,
  )?.task_root;
  const [uploadAttachment] = useUploadAttachmentMutation();
  const [selectLocalFile] = useSelectLocalFileMutation();
  const [attachLocalFile] = useAttachLocalFileMutation();
  const pendingRef = useRef(pending);
  pendingRef.current = pending;
  const parkedPendingRef = useRef(parked?.pending ?? []);
  parkedPendingRef.current = parked?.pending ?? [];
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
  const busy = disabled || sending || uploading;
  const canSend =
    !busy &&
    !selectionError && !settingsError &&
    modelReady &&
    !pending.some((item) => item.state === "error") &&
    parked === null &&
    (text.trim() !== "" || ready.length > 0);

  useEffect(() => {
    return () => {
      for (const item of pendingRef.current) {
        releasePreview(item);
      }
      for (const item of parkedPendingRef.current) {
        releasePreview(item);
      }
    };
  }, []);

  const sessionIdRef = useRef(sessionId);
  useEffect(() => {
    if (sessionIdRef.current === sessionId) {
      return;
    }
    sessionIdRef.current = sessionId;
    const previous = parkedRef.current;
    if (previous !== null) {
      for (const item of previous.pending) {
        releasePreview(item);
      }
    }
    parkedRef.current = null;
    setParked(null);
    flushingRef.current = false;
  }, [sessionId]);

  useEffect(() => {
    const wasRunning = wasRunningRef.current;
    wasRunningRef.current = running;
    if (wasRunning && !running && parked !== null && !sending) {
      void dispatchParked();
    }
  }, [running, parked, sending]);

  async function sendDraft(draft: ComposerDraft) {
    const done = draft.pending.filter(
      (item) => item.state === "done" && item.attachmentId !== null,
    );
    const content = composeSendContent(draft.text, done);
    const artifactRefs = done.map((item) => item.attachmentId as string);
    await onSendRef.current(content, artifactRefs);
    for (const item of draft.pending) {
      releasePreview(item);
    }
  }

  async function dispatchParked() {
    const draft = parkedRef.current;
    if (draft === null || flushingRef.current) {
      return;
    }
    flushingRef.current = true;
    parkedRef.current = null;
    setParked(null);
    try {
      await sendDraft(draft);
    } catch {
      parkedRef.current = draft;
      setParked(draft);
    } finally {
      flushingRef.current = false;
    }
  }

  async function submit(event?: FormEvent) {
    event?.preventDefault();
    if (!canSend) {
      return;
    }
    const draft: ComposerDraft = { text, pending };
    setText("");
    setPending([]);
    if (running) {
      parkedRef.current = draft;
      setParked(draft);
      return;
    }
    try {
      await sendDraft(draft);
    } catch {
      setText(draft.text);
      setPending(draft.pending);
    }
  }

  function restoreParked() {
    const draft = parkedRef.current;
    if (draft === null || flushingRef.current) {
      return;
    }
    parkedRef.current = null;
    const restored = restoreParkedDraft(draft, { text, pending });
    setParked(null);
    setText(restored.text);
    setPending(restored.pending);
    textareaRef.current?.focus();
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.nativeEvent.isComposing) {
      return;
    }
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
        sourcePath: null,
        error: null,
        previewUrl,
        attachmentId: null,
        state: "uploading",
      },
    ]);
    await upload(localId, file);
  }

  async function addLocalPath() {
    if (connectionId === null || disabled) {
      return;
    }
    try {
      const selection = await selectLocalFile().unwrap();
      if (selection.file === null) {
        return;
      }
      const chosen = selection.file;
      const localId = createClientId();
      setPending((current) => [
        ...current,
        {
          localId,
          name: chosen.name,
          kind: "file",
          file: null,
          size: 0,
          sourcePath: chosen.path,
          error: null,
          previewUrl: null,
          attachmentId: null,
          state: "uploading",
        },
      ]);
      await attachPath(localId, chosen.path);
    } catch (error) {
      setPending((current) => [
        ...current,
        {
          localId: createClientId(),
          name: "本机文件",
          kind: "file",
          file: null,
          size: 0,
          sourcePath: null,
          error: uploadError(error),
          previewUrl: null,
          attachmentId: null,
          state: "error",
        },
      ]);
    }
  }

  async function attachPath(localId: string, path: string) {
    if (connectionId === null || disabled) {
      return;
    }
    setPending((current) => current.map((item) =>
      item.localId === localId ? { ...item, state: "uploading", error: null } : item,
    ));
    try {
      const attached = await attachLocalFile({
        connectionId,
        sessionId,
        path,
      }).unwrap();
      setPending((current) =>
        current.map((item) =>
          item.localId === localId
            ? {
                ...item,
                attachmentId: attached.attachment_id,
                name: attached.name,
                size: attached.size,
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
    if (item.sourcePath !== null) {
      void attachPath(item.localId, item.sourcePath);
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
      className="composer"
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
      p={8}
      pl="md"
      radius="lg"
      shadow="lg"
      withBorder
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
      {parked === null ? null : (
        <Group className="composer-parked" gap={8} wrap="nowrap">
          <Text className="composer-parked-text" fz="sm" truncate>
            {parkedPreviewText(parked)}
          </Text>
          <Group gap={4} wrap="nowrap">
            <Button
              disabled={sending || disabled}
              loading={sending}
              onClick={() => void dispatchParked()}
              size="compact-xs"
              type="button"
              variant="subtle"
            >
              立即发送
            </Button>
            <Tooltip label="退回编辑栏">
              <ActionIcon
                aria-label="退回编辑栏"
                color="gray"
                disabled={sending || disabled}
                onClick={restoreParked}
                radius="xl"
                size={28}
                type="button"
                variant="subtle"
              >
                <IconTrash size={14} />
              </ActionIcon>
            </Tooltip>
          </Group>
        </Group>
      )}
      {pending.length === 0 ? null : (
        <Group className="composer-attachments" gap={8} wrap="wrap">
          {pending.map((item) => item.kind === "file" ? (
            <FileAttachmentTile key={item.localId} name={item.name} size={item.size}
              state={item.state} error={item.error}
              onRetry={disabled || (item.file === null && item.sourcePath === null) ? undefined : () => retryAttachment(item)}
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
      <Group className="composer-top" gap={10} wrap="nowrap" align="flex-end">
        <Textarea
          aria-label="消息"
          autosize
          className="composer-input"
          ref={textareaRef}
          value={text}
          onChange={(event) => setText(event.currentTarget.value)}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
          placeholder={disabled ? "正在连接…" : "输入消息，Enter 发送，可拖入文件或粘贴图片"}
          minRows={2}
          maxRows={7}
          variant="unstyled"
          disabled={disabled}
        />
        <Group gap={6} wrap="nowrap">
          {running && !paused ? (
            <Tooltip label="暂停">
              <ActionIcon
                aria-label="暂停"
                color="gray"
                disabled={pauseBusy || connectionId === null}
                onClick={() => onSetPaused(true)}
                radius="xl"
                size={36}
                type="button"
                variant="light"
              >
                <IconPlayerPauseFilled size={16} />
              </ActionIcon>
            </Tooltip>
          ) : null}
          {shouldWake && !running ? (
            <Tooltip label="继续">
              <ActionIcon
                aria-label="继续"
                color="blue"
                disabled={pauseBusy || retryBusy || connectionId === null}
                onClick={onRetry}
                radius="xl"
                size={36}
                type="button"
                variant="light"
              >
                <IconPlayerPlayFilled size={16} />
              </ActionIcon>
            </Tooltip>
          ) : null}
          <Tooltip label="发送">
            <ActionIcon
              aria-label="发送"
              disabled={!canSend}
              loading={sending}
              radius="xl"
              size={36}
              type="submit"
              variant="filled"
            >
              <IconArrowUp size={18} stroke={2.2} />
            </ActionIcon>
          </Tooltip>
        </Group>
      </Group>
      <Group className="composer-meta" justify="space-between" gap="sm">
        <Group gap={10} wrap="nowrap">
          <Tooltip label="添加本机文件，也可拖入或粘贴">
            <ActionIcon
              aria-label="Add attachment"
              disabled={busy || connectionId === null}
              onClick={() => void addLocalPath()}
              radius="xl"
              size={32}
              type="button"
              variant="subtle"
            >
              <IconPlus size={16} />
            </ActionIcon>
          </Tooltip>
          <Tooltip label="自动放行写文件等工具">
            <span style={{ display: "inline-flex" }}>
              <Switch
                aria-label="自动放行写文件等工具"
                checked={autoAuthorize}
                disabled={autoAuthorizeBusy || connectionId === null}
                onChange={(event) => onToggleAutoAuthorize(event.currentTarget.checked)}
                size="xs"
              />
            </span>
          </Tooltip>
        </Group>
        {workspacePath === undefined ? null : (
          <Text className="composer-workspace" c="dimmed" ff="monospace" fz={11}
            title={workspacePath} truncate>
            {workspacePath}
          </Text>
        )}
        <Group justify="flex-end" gap="sm" style={{ marginLeft: "auto" }}>
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
          <ModelSelector sessionId={sessionId} connectionId={connectionId} />
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
        stroke="var(--hm-ring-track)"
        strokeWidth="2"
      />
      <circle
        cx="7"
        cy="7"
        fill="none"
        r={radius}
        stroke="var(--mantine-color-sage-4)"
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
