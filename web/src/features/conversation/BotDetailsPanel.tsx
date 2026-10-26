import { ActionIcon, Text } from "@mantine/core";
import { useMediaQuery } from "@mantine/hooks";
import { IconLayoutSidebarRightCollapse } from "@tabler/icons-react";
import { useEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent, type ReactNode } from "react";

const WIDTH_KEY = "konden.botDetailsWidth";
const MIN_WIDTH = 220;
const MAX_WIDTH = 520;
const MIN_CONVERSATION_WIDTH = 480;

function readWidth() {
  const saved = window.localStorage.getItem(WIDTH_KEY);
  if (saved !== null && Number.isFinite(Number(saved))) return Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, Number(saved)));
  return null;
}

export function BotDetailsPanel({ onClose, children }: { onClose: () => void; children: ReactNode }) {
  const [width, setWidth] = useState(readWidth);
  const wide = useMediaQuery("(min-width: 1700px)", false, { getInitialValueInEffect: false });
  const preferredWidth = width ?? (wide ? 290 : 264);
  const [maxWidth, setMaxWidth] = useState(MAX_WIDTH);
  const [resizing, setResizing] = useState(false);
  const panelRef = useRef<HTMLElement>(null);
  const widthRef = useRef(preferredWidth);
  const dragPointer = useRef<number | null>(null);

  function availableWidth() {
    return Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, panelRef.current!.parentElement!.getBoundingClientRect().width - MIN_CONVERSATION_WIDTH));
  }

  useEffect(() => {
    const parent = panelRef.current!.parentElement!;
    const updateMaxWidth = () => setMaxWidth(availableWidth());
    updateMaxWidth();
    const observer = new ResizeObserver(updateMaxWidth);
    observer.observe(parent);
    return () => {
      observer.disconnect();
      document.body.classList.remove("is-resizing-bot-details");
    };
  }, []);

  function changeWidth(next: number) {
    widthRef.current = Math.max(MIN_WIDTH, Math.min(availableWidth(), next));
    setWidth(widthRef.current);
  }

  function beginResize(event: PointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    widthRef.current = panelRef.current!.getBoundingClientRect().width;
    dragPointer.current = event.pointerId;
    setResizing(true);
    document.body.classList.add("is-resizing-bot-details");
  }

  function moveResize(event: PointerEvent<HTMLDivElement>) {
    if (dragPointer.current !== event.pointerId) return;
    changeWidth(panelRef.current!.parentElement!.getBoundingClientRect().right - event.clientX);
  }

  function endResize() {
    if (dragPointer.current === null) return;
    dragPointer.current = null;
    setResizing(false);
    document.body.classList.remove("is-resizing-bot-details");
    window.localStorage.setItem(WIDTH_KEY, String(widthRef.current));
  }

  function resizeWithKeyboard(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    changeWidth(panelRef.current!.getBoundingClientRect().width + (event.key === "ArrowLeft" ? 20 : -20));
    window.localStorage.setItem(WIDTH_KEY, String(widthRef.current));
  }

  return <aside className="agent-details bot-details-panel" aria-label="Agent 资料" ref={panelRef}
    style={{ "--bot-details-width": `${preferredWidth}px` } as CSSProperties}>
    <div role="separator" aria-label="调节 Bot 资料宽度" aria-orientation="vertical" tabIndex={0}
      aria-valuemin={MIN_WIDTH} aria-valuemax={maxWidth} aria-valuenow={Math.min(preferredWidth, maxWidth)}
      className={resizing ? "bot-details-resize is-dragging" : "bot-details-resize"}
      onPointerDown={beginResize} onPointerMove={moveResize} onPointerUp={endResize}
      onPointerCancel={endResize} onLostPointerCapture={endResize} onKeyDown={resizeWithKeyboard} />
    <div className="bot-details-scroll">
      <header className="details-heading"><Text fw={600} fz={12}>Bot 资料</Text>
        <ActionIcon aria-label="关闭 Bot 资料" variant="subtle" color="gray" onClick={onClose}><IconLayoutSidebarRightCollapse size={17} /></ActionIcon>
      </header>
      {children}
    </div>
  </aside>;
}
