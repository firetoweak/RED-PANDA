import { ActionIcon, Alert, Badge, Button, Center, Group, Loader, Paper, ScrollArea, Stack, Text } from "@mantine/core";
import { IconArrowDown, IconX } from "@tabler/icons-react";
import { useEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent } from "react";

import { useObserveSubagentQuery } from "../../api/redpandaApi";
import { useAppSelector } from "../../app/hooks";
import { StepDisclosure } from "./ExecutionProcess";
import { MarkdownMessage } from "./MarkdownMessage";
import { useFollowOutput } from "./useFollowOutput";
import { visibleTimeline } from "./visibleTimeline";

const WIDTH_KEY = "redpanda.subagentPanelWidth";
const DEFAULT_WIDTH = 600;
const MIN_WIDTH = 320;

function readPanelWidth() {
  const raw = window.localStorage.getItem(WIDTH_KEY);
  const value = raw === null ? DEFAULT_WIDTH : Number(raw);
  return Number.isFinite(value) && value >= MIN_WIDTH ? value : DEFAULT_WIDTH;
}

export function SubagentPanel({ parentSessionId, commandId, onClose }: {
  parentSessionId: string; commandId: string; onClose: () => void;
}) {
  const [width, setWidth] = useState(readPanelWidth);
  const [maxWidth, setMaxWidth] = useState(window.innerWidth - MIN_WIDTH);
  const [resizing, setResizing] = useState(false);
  const panelRef = useRef<HTMLElement>(null);
  const widthRef = useRef(width);
  const dragPointer = useRef<number | null>(null);
  useEffect(() => {
    const parent = panelRef.current!.parentElement!;
    const updateMaxWidth = () => setMaxWidth(Math.max(MIN_WIDTH, parent.getBoundingClientRect().width - MIN_WIDTH));
    updateMaxWidth();
    const observer = new ResizeObserver(updateMaxWidth);
    observer.observe(parent);
    return () => {
      observer.disconnect();
      document.body.classList.remove("is-resizing-subagent");
    };
  }, []);

  function changeWidth(next: number) {
    const available = panelRef.current!.parentElement!.getBoundingClientRect().width - MIN_WIDTH;
    widthRef.current = Math.max(MIN_WIDTH, Math.min(available, next));
    setWidth(widthRef.current);
  }

  function beginResize(event: PointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    dragPointer.current = event.pointerId;
    setResizing(true);
    document.body.classList.add("is-resizing-subagent");
  }

  function moveResize(event: PointerEvent<HTMLDivElement>) {
    if (dragPointer.current !== event.pointerId) return;
    changeWidth(panelRef.current!.parentElement!.getBoundingClientRect().right - event.clientX);
  }

  function endResize() {
    if (dragPointer.current === null) return;
    dragPointer.current = null;
    setResizing(false);
    document.body.classList.remove("is-resizing-subagent");
    window.localStorage.setItem(WIDTH_KEY, String(widthRef.current));
  }

  function resizeWithKeyboard(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    changeWidth(panelRef.current!.getBoundingClientRect().width + (event.key === "ArrowLeft" ? 20 : -20));
    window.localStorage.setItem(WIDTH_KEY, String(widthRef.current));
  }

  const query = useObserveSubagentQuery({ parentSessionId, commandId });
  const observation = query.currentData;
  const runtime = useAppSelector((state) => observation === undefined ? undefined : state.runtime.sessions[observation.session_id]);
  const connected = useAppSelector((state) => state.runtime.connectionId !== null);
  const activity = runtime?.activity ?? observation?.activity;
  const result = observation?.result ?? null;
  const live = connected && (activity === "running" || runtime?.activePreview != null || runtime?.activeThinking != null);
  const follow = useFollowOutput(observation?.session_id, live, observation?.conversation != null);
  const conversation = observation?.conversation;
  const items = conversation == null ? [] : visibleTimeline(
    conversation, runtime?.committed ?? {}, runtime?.activePreview ?? null, runtime?.tools ?? {},
    runtime?.committedThinking ?? {}, runtime?.activeThinking ?? null, runtime?.activity ?? null,
  );
  const label = result !== null
    ? result.cancelled ? "已收回" : result.failure !== null ? "执行失败" : result.reported ? "已交回" : "已结束，未报告"
    : !connected ? "连接中断" : conversation === null ? "正在创建" : activity === "running" ? "运行中" : "等待中";

  return <section className="subagent-panel" aria-label="子 Agent 观察面板" ref={panelRef}
    style={{ "--subagent-panel-width": `${width}px` } as CSSProperties}>
    <div role="separator" aria-label="调节子 Agent 面板宽度" aria-orientation="vertical"
      aria-valuemin={MIN_WIDTH} aria-valuemax={maxWidth} aria-valuenow={Math.min(width, maxWidth)} tabIndex={0}
      className={resizing ? "subagent-panel-resize is-dragging" : "subagent-panel-resize"}
      onPointerDown={beginResize} onPointerMove={moveResize} onPointerUp={endResize}
      onPointerCancel={endResize} onLostPointerCapture={endResize} onKeyDown={resizeWithKeyboard} />
    <Stack className="subagent-panel-header" gap={8}>
      <Group className="subagent-panel-controls" justify="space-between" wrap="nowrap">
        <Group gap="xs"><Text fw={600}>子 Agent</Text><Badge variant="light">{label}</Badge></Group>
        <ActionIcon aria-label="关闭子 Agent 面板" onClick={onClose} variant="subtle"><IconX size={17} /></ActionIcon>
      </Group>
      {observation === undefined ? null : <Text className="pre-wrap" fz={12} c="dimmed">{observation.task}</Text>}
    </Stack>
    {query.isError ? <Alert color="red" m="md">无法读取子任务过程</Alert>
      : observation === undefined ? <Center><Loader size="sm" /></Center>
      : <ScrollArea className="subagent-panel-scroll" viewportRef={follow.viewportRef} type="hover">
        <Stack className="subagent-panel-content" gap="md" ref={follow.contentRef}>
          {items.length === 0 ? <Text c="dimmed" size="sm">{conversation === null ? "正在创建子任务…" : "尚无执行记录"}</Text> : null}
          {items.length === 0 ? null : <Paper className="execution-process subagent-process" withBorder radius="md">
            <Stack className="execution-steps" gap={4}>
              {items.map((item, index) => item.kind === "user"
                ? <MarkdownMessage key={item.key} content={item.text} streaming={false} />
                : <StepDisclosure key={item.key} step={item} index={index} expandRunning />)}
            </Stack>
          </Paper>}
          {runtime?.lastError == null ? null : <Alert color="red">{runtime.lastError}</Alert>}
          {result?.summary == null ? null : <Paper withBorder radius="md" p="sm">
            <Text fz={12} fw={600} mb="xs">交回结论</Text><MarkdownMessage content={result.summary} streaming={false} />
          </Paper>}
          {result?.failure == null ? null : <Alert color="red" title="执行失败">{result.failure}</Alert>}
          {result?.reason == null ? null : <Text c="dimmed" size="sm">收回原因：{result.reason}</Text>}
        </Stack>
      </ScrollArea>}
    {follow.following ? null : <Button className="subagent-jump" size="compact-sm" variant="light"
      leftSection={<IconArrowDown size={14} />} onClick={follow.scrollToBottom}>回到最新</Button>}
  </section>;
}
