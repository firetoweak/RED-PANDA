import { useCallback, useEffect, useRef, type ReactNode, type RefObject } from "react";
import { Virtuoso, type VirtuosoHandle, type StateSnapshot } from "react-virtuoso";

import type { TimelineTurn } from "./timelineTurns";

const components = {
  Header: () => <div className="timeline-start" />,
  Footer: () => <div className="timeline-end" />,
};
const readingPositions = new Map<string, StateSnapshot>();

type ConversationTimelineProps = {
  conversationKey?: string;
  turns: TimelineTurn[];
  listRef: RefObject<VirtuosoHandle | null>;
  wheelAreaRef: RefObject<HTMLDivElement | null>;
  following: boolean;
  onFollowingChange: (following: boolean) => void;
  renderTurn: (turn: TimelineTurn) => ReactNode;
};

export function ConversationTimeline({
  conversationKey, turns, listRef, wheelAreaRef, following, onFollowingChange, renderTurn,
}: ConversationTimelineProps) {
  const restored = useRef(conversationKey === undefined ? undefined : readingPositions.get(conversationKey));
  const viewportRef = useRef<HTMLElement | null>(null);
  const sticking = useRef(following);
  sticking.current = following;
  const frame = useRef<number | null>(null);

  const followHeight = useCallback(() => {
    if (!sticking.current) return;
    if (frame.current !== null) cancelAnimationFrame(frame.current);
    frame.current = requestAnimationFrame(() => {
      frame.current = null;
      // 新增高度不能被误判成用户主动离开底部。
      const viewport = viewportRef.current;
      if (sticking.current && viewport !== null) viewport.scrollTop = viewport.scrollHeight;
    });
  }, []);

  useEffect(() => {
    const area = wheelAreaRef.current;
    const viewport = viewportRef.current!;
    const measureDock = () => {
      viewport.style.setProperty("--composer-height", `${area!.getBoundingClientRect().height}px`);
    };
    measureDock();
    const dockObserver = new ResizeObserver(measureDock);
    dockObserver.observe(area!);
    let previousTop = viewport.scrollTop;
    const stopFollowing = () => {
      sticking.current = false;
      onFollowingChange(false);
    };
    const onWheel = (event: WheelEvent) => {
      if (!event.ctrlKey && event.deltaY < 0) stopFollowing();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (["Home", "PageUp", "ArrowUp"].includes(event.key) || (event.key === " " && event.shiftKey)) stopFollowing();
    };
    const onPointerDown = (event: PointerEvent) => {
      if (event.target === viewport) stopFollowing();
    };
    const onScroll = () => {
      if (conversationKey !== undefined) {
        const scrollTop = viewport.scrollTop - (viewport.querySelector(".timeline-start")?.clientHeight ?? 0);
        listRef.current?.getState((state) => readingPositions.set(conversationKey, { ...state, scrollTop }));
      }
      const movedDown = viewport.scrollTop > previousTop;
      previousTop = viewport.scrollTop;
      if (movedDown && viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight <= 24) {
        sticking.current = true;
        onFollowingChange(true);
      }
    };
    const onAreaWheel = (event: WheelEvent) => {
      if (event.defaultPrevented || event.ctrlKey || event.deltaY === 0) return;
      const target = event.target;
      if (target instanceof HTMLTextAreaElement && (
        (event.deltaY < 0 && target.scrollTop > 0)
        || (event.deltaY > 0 && target.scrollTop + target.clientHeight < target.scrollHeight)
      )) return;
      event.preventDefault();
      onWheel(event);
      const unit = event.deltaMode === WheelEvent.DOM_DELTA_LINE ? 16
        : event.deltaMode === WheelEvent.DOM_DELTA_PAGE ? viewport.clientHeight : 1;
      viewport.scrollTop += event.deltaY * unit;
    };
    viewport.addEventListener("wheel", onWheel, { passive: true });
    viewport.addEventListener("keydown", onKeyDown);
    viewport.addEventListener("pointerdown", onPointerDown);
    viewport.addEventListener("touchstart", stopFollowing, { passive: true });
    viewport.addEventListener("scroll", onScroll, { passive: true });
    area?.addEventListener("wheel", onAreaWheel, { passive: false });
    return () => {
      viewport.removeEventListener("wheel", onWheel);
      viewport.removeEventListener("keydown", onKeyDown);
      viewport.removeEventListener("pointerdown", onPointerDown);
      viewport.removeEventListener("touchstart", stopFollowing);
      viewport.removeEventListener("scroll", onScroll);
      area?.removeEventListener("wheel", onAreaWheel);
      dockObserver.disconnect();
      if (frame.current !== null) cancelAnimationFrame(frame.current);
    };
  }, [conversationKey, listRef, onFollowingChange, wheelAreaRef]);

  return (
    <Virtuoso
      ref={listRef}
      className="timeline-scroll virtual-timeline"
      data={turns}
      computeItemKey={(_, turn) => turn.key}
      {...(restored.current === undefined
        ? { initialTopMostItemIndex: { index: "LAST" as const, align: "end" as const } }
        : { restoreStateFrom: restored.current })}
      increaseViewportBy={{ top: 800, bottom: 800 }}
      atBottomThreshold={24}
      totalListHeightChanged={followHeight}
      components={components}
      scrollerRef={(element) => {
        viewportRef.current = element instanceof HTMLElement ? element : null;
      }}
      itemContent={(_, turn) => (
        <div className="timeline-turn" data-turn-key={turn.key}>
          {renderTurn(turn)}
        </div>
      )}
    />
  );
}
