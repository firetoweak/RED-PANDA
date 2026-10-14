import { useCallback, useLayoutEffect, useRef, useState } from "react";

// 判定"贴底"的容差，用来吸收亚像素、缩放和滚动条取整误差。
const BOTTOM_THRESHOLD = 24;

export function useFollowOutput(
  sessionId: string | undefined,
  active: boolean,
  attached: boolean,
) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const wheelAreaRef = useRef<HTMLDivElement>(null);
  const lastSessionId = useRef<string | undefined>(undefined);
  // 是否仍吸附在底部。用户上滑离开底部后为 false，此时不再自动滚动。
  const [following, setFollowing] = useState(true);
  // following 的同步镜像：ResizeObserver 回调发生在内容变高之后，
  // 那时实时测量必然"不贴底"，只能读取变化前的跟随意图。
  const sticking = useRef(true);

  const isAtBottom = useCallback((viewport: HTMLElement) => {
    return (
      viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight <=
      BOTTOM_THRESHOLD
    );
  }, []);

  // 供"跳到最新"入口调用：滚到底并恢复跟随。
  const scrollToBottom = useCallback(() => {
    const viewport = viewportRef.current;
    if (viewport === null) {
      return;
    }
    sticking.current = true;
    setFollowing(true);
    viewport.scrollTop = viewport.scrollHeight;
  }, []);

  useLayoutEffect(() => {
    if (!attached) {
      return;
    }
    const viewport = viewportRef.current;
    const content = contentRef.current;
    if (viewport === null || content === null) {
      return;
    }

    const settleFollowing = (next: boolean) => {
      sticking.current = next;
      setFollowing((prev) => (prev === next ? prev : next));
    };
    const syncFollowing = () => {
      settleFollowing(isAtBottom(viewport));
    };

    const sessionChanged = lastSessionId.current !== sessionId;
    lastSessionId.current = sessionId;

    // 新会话或新一轮流式开始时贴到底部；否则按当前位置同步跟随状态。
    if (active || sessionChanged) {
      sticking.current = true;
      setFollowing(true);
      viewport.scrollTop = viewport.scrollHeight;
    } else {
      syncFollowing();
    }

    const onScroll = () => {
      syncFollowing();
    };
    const onWheel = (event: WheelEvent) => {
      // 向上滚的意图比 scroll 事件到得更早，先解除跟随，避免"刚上滑又被拉回"。
      if (event.deltaY < 0 && sticking.current) {
        settleFollowing(false);
      }
    };

    viewport.addEventListener("scroll", onScroll, { passive: true });
    viewport.addEventListener("wheel", onWheel, { passive: true });

    // 固定计划区不在视口内，滚轮操作仍用于浏览对话。
    const wheelArea = wheelAreaRef.current;
    const onAreaWheel = (event: WheelEvent) => {
      if (event.ctrlKey || event.deltaY === 0) {
        return;
      }
      event.preventDefault();
      onWheel(event);
      const unit = event.deltaMode === WheelEvent.DOM_DELTA_LINE ? 16
        : event.deltaMode === WheelEvent.DOM_DELTA_PAGE ? viewport.clientHeight : 1;
      viewport.scrollTop += event.deltaY * unit;
      syncFollowing();
    };
    wheelArea?.addEventListener("wheel", onAreaWheel, { passive: false });

    let observer: ResizeObserver | undefined;
    if (active) {
      // 内容长高时，只有"变化前就贴着底部"才跟随；否则保持用户当前位置。
      observer = new ResizeObserver(() => {
        if (sticking.current) {
          viewport.scrollTop = viewport.scrollHeight;
        }
      });
      observer.observe(content);
    }

    return () => {
      viewport.removeEventListener("scroll", onScroll);
      viewport.removeEventListener("wheel", onWheel);
      wheelArea?.removeEventListener("wheel", onAreaWheel);
      observer?.disconnect();
    };
  }, [active, attached, isAtBottom, sessionId]);

  return { contentRef, viewportRef, wheelAreaRef, following, scrollToBottom };
}
