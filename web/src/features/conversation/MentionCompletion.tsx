import { Text } from "@mantine/core";
import { IconUsersGroup } from "@tabler/icons-react";
import { useId, useLayoutEffect, useRef, useState, type ChangeEvent, type CompositionEvent, type FocusEvent, type KeyboardEvent, type RefObject, type SyntheticEvent } from "react";
import { BotAvatar } from "./ChatPresentation";

/** 私聊与群聊共用填名交互；是否通知、通知谁仍由各自发送入口决定。 */
export function useMentionCompletion({ text, onChange, names, textarea, includeAll = false }: {
  text: string;
  onChange: (text: string) => void;
  names: string[];
  textarea: RefObject<HTMLTextAreaElement | null>;
  includeAll?: boolean;
}) {
  const [cursor, setCursor] = useState(0);
  const [dismissed, setDismissed] = useState(false);
  const [selected, setSelected] = useState(0);
  const [composing, setComposing] = useState(false);
  const nextCursor = useRef<number | null>(null);
  const list = useRef<HTMLDivElement>(null);
  const activeOption = useRef<HTMLButtonElement>(null);
  const listId = useId();
  const match = text.slice(0, cursor).match(/(^|[^A-Za-z0-9._%+-])@([^\n@]*)$/u);
  const candidates = match === null ? [] : [
    ...(includeAll ? [{ value: "all", name: "所有人", all: true }] : []),
    ...names.map((name) => ({ value: name, name, all: false })),
  ].filter(({ value, name }) => [value, name].some((label) => label.toLocaleLowerCase().startsWith(match[2].toLocaleLowerCase())));
  const opened = !dismissed && !composing && candidates.length > 0;
  const activeIndex = Math.min(selected, candidates.length - 1);

  useLayoutEffect(() => {
    if (nextCursor.current === null) return;
    textarea.current?.focus();
    textarea.current?.setSelectionRange(nextCursor.current, nextCursor.current);
    nextCursor.current = null;
  }, [text, textarea]);

  useLayoutEffect(() => {
    const option = activeOption.current;
    const menu = list.current;
    if (option === null || menu === null) return;
    const bottom = option.offsetTop + option.offsetHeight;
    if (option.offsetTop < menu.scrollTop) menu.scrollTop = option.offsetTop;
    else if (bottom > menu.scrollTop + menu.clientHeight) menu.scrollTop = bottom - menu.clientHeight;
  }, [activeIndex, opened, match?.[2]]);

  function replace(start: number, end: number, value: string) {
    const position = start + value.length;
    nextCursor.current = position;
    onChange(text.slice(0, start) + value + text.slice(end));
    setCursor(position);
    setSelected(0);
    setDismissed(false);
  }

  function choose(name: string) {
    if (match === null) return;
    replace(cursor - match[2].length - 1, textarea.current?.selectionEnd ?? cursor, `@${name} `);
    setDismissed(true);
  }

  return {
    inputProps: {
      "aria-autocomplete": "list" as const,
      "aria-controls": opened ? listId : undefined,
      "aria-activedescendant": opened ? `${listId}-${activeIndex}` : undefined,
      onChange: (event: ChangeEvent<HTMLTextAreaElement>) => {
        onChange(event.currentTarget.value);
        setCursor(event.currentTarget.selectionStart);
        setDismissed(false);
        setSelected(0);
      },
      onSelect: (event: SyntheticEvent<HTMLTextAreaElement>) => setCursor(event.currentTarget.selectionStart),
      onFocus: (event: FocusEvent<HTMLTextAreaElement>) => {
        setCursor(event.currentTarget.selectionStart);
        setDismissed(false);
      },
      onBlur: () => setDismissed(true),
      onCompositionStart: () => setComposing(true),
      onCompositionEnd: (event: CompositionEvent<HTMLTextAreaElement>) => {
        setComposing(false);
        setCursor(event.currentTarget.selectionStart);
      },
    },
    handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
      if (event.nativeEvent.isComposing || composing) return true;
      if (!opened || event.shiftKey || !["ArrowDown", "ArrowUp", "Enter", "Tab", "Escape"].includes(event.key)) return false;
      event.preventDefault();
      if (event.key === "Escape") setDismissed(true);
      else if (event.key === "ArrowDown") setSelected((activeIndex + 1) % candidates.length);
      else if (event.key === "ArrowUp") setSelected((activeIndex - 1 + candidates.length) % candidates.length);
      else choose(candidates[activeIndex].value);
      return true;
    },
    insertTrigger() {
      const start = textarea.current?.selectionStart ?? text.length;
      const end = textarea.current?.selectionEnd ?? start;
      const prefix = start > 0 && /[A-Za-z0-9._%+-]/.test(text[start - 1]) ? " @" : "@";
      replace(start, end, prefix);
    },
    menu: opened ? <div className="mention-menu">
      <Text className="mention-heading" fz={11} c="dimmed">{includeAll ? "点名群成员" : "选择 Bot"}</Text>
      <div role="listbox" aria-label="点名候选" id={listId} ref={list}>
        {candidates.map(({ value, name, all }, index) => <button key={`${all ? "all" : "member"}:${value}`} type="button" role="option"
          id={`${listId}-${index}`} aria-selected={index === activeIndex} tabIndex={-1}
          ref={index === activeIndex ? activeOption : undefined}
          onMouseDown={(event) => event.preventDefault()} onClick={() => choose(value)}>
          {all ? <span className="agent-avatar agent-avatar-small" aria-hidden="true"><IconUsersGroup size={16} /></span> : <BotAvatar name={name} className="agent-avatar-small" />}
          <span className="mention-name">{name}</span><span className="mention-at">@{value}</span>
        </button>)}
      </div>
    </div> : null,
  };
}
