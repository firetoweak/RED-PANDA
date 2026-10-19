import { ActionIcon, Group, Text } from "@mantine/core";
import { IconX } from "@tabler/icons-react";
import { useEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent, type ReactNode } from "react";

const WIDTH_KEY = "redpanda.contentPageWidth";
const MIN_WIDTH = 320;
const MIN_CONVERSATION_WIDTH = 480;
const DEFAULT_WIDTH = 640;

function readWidth() {
  const value = Number(window.localStorage.getItem(WIDTH_KEY) ?? DEFAULT_WIDTH);
  return Number.isFinite(value) && value >= MIN_WIDTH ? value : DEFAULT_WIDTH;
}

export function ContentPage({ title, onClose, children }: {
  title: string; onClose: () => void; children: ReactNode;
}) {
  const [width, setWidth] = useState(readWidth);
  const [maxWidth, setMaxWidth] = useState(MIN_WIDTH);
  const [resizing, setResizing] = useState(false);
  const panelRef = useRef<HTMLElement>(null);
  const widthRef = useRef(width);
  const dragPointer = useRef<number | null>(null);

  useEffect(() => {
    const parent = panelRef.current!.parentElement!;
    const updateMaxWidth = () => setMaxWidth(Math.max(MIN_WIDTH, parent.getBoundingClientRect().width - MIN_CONVERSATION_WIDTH));
    updateMaxWidth();
    const observer = new ResizeObserver(updateMaxWidth);
    observer.observe(parent);
    return () => {
      observer.disconnect();
      document.body.classList.remove("is-resizing-content-page");
    };
  }, []);

  function changeWidth(next: number) {
    const available = Math.max(MIN_WIDTH, panelRef.current!.parentElement!.getBoundingClientRect().width - MIN_CONVERSATION_WIDTH);
    widthRef.current = Math.max(MIN_WIDTH, Math.min(available, next));
    setWidth(widthRef.current);
  }

  function beginResize(event: PointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    dragPointer.current = event.pointerId;
    setResizing(true);
    document.body.classList.add("is-resizing-content-page");
  }

  function moveResize(event: PointerEvent<HTMLDivElement>) {
    if (dragPointer.current !== event.pointerId) return;
    changeWidth(panelRef.current!.parentElement!.getBoundingClientRect().right - event.clientX);
  }

  function endResize() {
    if (dragPointer.current === null) return;
    dragPointer.current = null;
    setResizing(false);
    document.body.classList.remove("is-resizing-content-page");
    window.localStorage.setItem(WIDTH_KEY, String(widthRef.current));
  }

  function resizeWithKeyboard(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    changeWidth(panelRef.current!.getBoundingClientRect().width + (event.key === "ArrowLeft" ? 20 : -20));
    window.localStorage.setItem(WIDTH_KEY, String(widthRef.current));
  }

  return <section className="content-page" aria-label={title} ref={panelRef}
    style={{ "--content-page-width": `${width}px` } as CSSProperties}>
    <div role="separator" aria-label="调节内容页宽度" aria-orientation="vertical"
      aria-valuemin={MIN_WIDTH} aria-valuemax={maxWidth} aria-valuenow={Math.min(width, maxWidth)} tabIndex={0}
      className={resizing ? "content-page-resize is-dragging" : "content-page-resize"}
      onPointerDown={beginResize} onPointerMove={moveResize} onPointerUp={endResize}
      onPointerCancel={endResize} onLostPointerCapture={endResize} onKeyDown={resizeWithKeyboard} />
    <Group className="content-page-header" justify="space-between" wrap="nowrap">
      <Text className="content-page-title" fw={600} size="sm" truncate>{title}</Text>
      <ActionIcon aria-label="关闭内容页" onClick={onClose} variant="subtle"><IconX size={17} /></ActionIcon>
    </Group>
    <div className="content-page-body">{children}</div>
  </section>;
}
