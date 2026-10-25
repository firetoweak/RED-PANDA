import { useCallback, useLayoutEffect, useRef, useState } from "react";
type ReadingMessage = { message_id: string };

type ReadingPosition = { anchor: string | null; offset: number; bottom: boolean; lastRead: string | null };
const positions = new Map<string, ReadingPosition>();
const EMPTY_MESSAGES: readonly ReadingMessage[] = [];

/** 阅读位置属于对话；短暂查看资料、私聊或引用不改变原来的补读位置。 */
export function useMessageReading(groupId: string, messages: readonly ReadingMessage[] = EMPTY_MESSAGES) {
  const viewport = useRef<HTMLDivElement>(null);
  const initial = useRef(positions.get(groupId));
  const initialized = useRef(false);
  const restoring = useRef<ReadingPosition | null>(null);
  const programmatic = useRef(false);
  const following = useRef(initial.current?.bottom ?? true);
  const lastRead = useRef(initial.current?.lastRead ?? null);
  const currentMessages = useRef(messages);
  currentMessages.current = messages;
  const [atBottom, setAtBottom] = useState(following.current);
  const [unreadAfter, setUnreadAfter] = useState(initial.current?.lastRead ?? null);
  const [pending, setPending] = useState(0);
  const [returnTop, setReturnTop] = useState<number | null>(null);

  const save = useCallback(() => {
    const element = viewport.current;
    if (element === null || !initialized.current || restoring.current !== null) return;
    const top = element.getBoundingClientRect().top;
    const anchor = Array.from(element.querySelectorAll<HTMLElement>("[data-message-id]"))
      .find((item) => item.getBoundingClientRect().bottom > top + 1);
    positions.set(groupId, { anchor: anchor?.dataset.messageId ?? null,
      offset: anchor === undefined ? 0 : anchor.getBoundingClientRect().top - top,
      bottom: following.current, lastRead: lastRead.current });
  }, [groupId]);

  const move = useCallback((top: number) => {
    const element = viewport.current;
    if (element === null) return;
    programmatic.current = true;
    element.scrollTop = top;
    requestAnimationFrame(() => { programmatic.current = false; save(); });
  }, [save]);

  const latest = useCallback(() => {
    restoring.current = null;
    following.current = true;
    setAtBottom(true); setPending(0); setUnreadAfter(null); setReturnTop(null);
    lastRead.current = currentMessages.current.at(-1)?.message_id ?? null;
    if (viewport.current !== null) move(viewport.current.scrollHeight);
  }, [move]);

  useLayoutEffect(() => {
    const element = viewport.current;
    if (element === null) return;
    if (!initialized.current) {
      initialized.current = true;
      const position = initial.current;
      if (position !== undefined && !position.bottom) {
        restoring.current = position;
        const anchor = Array.from(element.querySelectorAll<HTMLElement>("[data-message-id]"))
          .find((item) => item.dataset.messageId === position.anchor);
        if (anchor !== undefined) move(anchor.getBoundingClientRect().top - element.getBoundingClientRect().top + element.scrollTop - position.offset);
      } else latest();
    } else if (following.current) latest();
    if (!following.current) {
      const index = messages.findIndex((message) => message.message_id === lastRead.current);
      const count = index < 0 ? 0 : messages.length - index - 1;
      setPending(count);
      if (count > 0) setUnreadAfter((current) => current ?? lastRead.current);
    }
  }, [messages, latest, move]);

  useLayoutEffect(() => {
    const element = viewport.current;
    const transcript = element?.querySelector<HTMLElement>(".group-transcript");
    if (element === null || transcript == null) return;
    // Markdown 与代码块会在首帧后测量；在用户开始滚动前继续贴住原来的段落。
    const restore = () => {
      const position = restoring.current;
      if (position === null) return;
      const anchor = Array.from(transcript.querySelectorAll<HTMLElement>("[data-message-id]"))
        .find((item) => item.dataset.messageId === position.anchor);
      if (anchor !== undefined) move(element.scrollTop + anchor.getBoundingClientRect().top - element.getBoundingClientRect().top - position.offset);
    };
    const release = () => { restoring.current = null; };
    const observer = new ResizeObserver(restore);
    observer.observe(transcript);
    element.addEventListener("wheel", release, { passive: true });
    element.addEventListener("touchstart", release, { passive: true });
    element.addEventListener("keydown", release);
    element.addEventListener("pointerdown", release);
    return () => {
      observer.disconnect();
      element.removeEventListener("wheel", release);
      element.removeEventListener("touchstart", release);
      element.removeEventListener("keydown", release);
      element.removeEventListener("pointerdown", release);
    };
  }, [messages.length > 0, move]);

  function onScroll(y: number) {
    const element = viewport.current;
    if (element === null || programmatic.current || restoring.current !== null) return;
    const bottom = element.scrollHeight - element.clientHeight - y <= 32;
    following.current = bottom;
    setAtBottom(bottom);
    if (bottom) {
      lastRead.current = currentMessages.current.at(-1)?.message_id ?? null;
      setPending(0); setUnreadAfter(null);
    }
    save();
  }

  function jump(id: string) {
    const element = viewport.current;
    const target = Array.from(element?.querySelectorAll<HTMLElement>("[data-message-id]") ?? [])
      .find((item) => item.dataset.messageId === id);
    if (element === null || target === undefined) return;
    setReturnTop((current) => current ?? element.scrollTop);
    following.current = false; setAtBottom(false);
    restoring.current = null;
    move(element.scrollTop + target.getBoundingClientRect().top - element.getBoundingClientRect().top - 20);
    target.classList.add("is-quote-target");
    window.setTimeout(() => target.classList.remove("is-quote-target"), 2200);
  }

  return { viewport, atBottom, pending, unreadAfter, onScroll, latest, jump,
    canReturn: returnTop !== null,
    returnToReading: () => { if (returnTop !== null) move(returnTop); setReturnTop(null); } };
}
