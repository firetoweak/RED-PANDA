import { ActionIcon, Button, Group, Text, Textarea } from "@mantine/core";
import { IconAt, IconArrowUp, IconUsersGroup } from "@tabler/icons-react";
import { useContext, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { ChatPresentation, BotAvatar, MessageQuote, splitMessageQuote, withMessageQuote } from "../conversation/ChatPresentation";
import { clearSentComposerText, restoreComposerText, saveComposerText } from "../conversation/composerDraft";

export function GroupComposer({ groupId, groupName, members, connected, posting, onSend }: {
  groupId?: string;
  groupName: string;
  members: string[];
  connected: boolean;
  posting: boolean;
  onSend: (text: string, mention: string[]) => Promise<boolean>;
}) {
  const draftKey = `group:${groupId ?? groupName}`;
  const [draft, setDraft] = useState(() => restoreComposerText(draftKey));
  const { quote: requestedQuote, onQuote } = useContext(ChatPresentation);
  const { body: text, quote } = splitMessageQuote(draft);
  const setText = (body: string) => setDraft(withMessageQuote(body, quote));
  useEffect(() => { saveComposerText(draftKey, draft, ""); }, [draftKey, draft]);
  useEffect(() => {
    if (requestedQuote === null) return;
    setDraft((current) => withMessageQuote(splitMessageQuote(current).body, requestedQuote));
    onQuote(null);
    textarea.current?.focus();
  }, [requestedQuote, onQuote]);
  const [cursor, setCursor] = useState(0);
  const [dismissed, setDismissed] = useState(false);
  const [selected, setSelected] = useState(0);
  const [composing, setComposing] = useState(false);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const nextCursor = useRef<number | null>(null);
  const listId = useId();
  const match = text.slice(0, cursor).match(/(^|[^A-Za-z0-9._%+-])@([^\n@]*)$/u);
  const candidates = match === null ? [] : [
    { value: "all", name: "所有人", all: true },
    ...members.map((name) => ({ value: name, name, all: false })),
  ].filter(({ value, name }) => [value, name].some((label) => label.toLocaleLowerCase().startsWith(match[2].toLocaleLowerCase())));
  const opened = !dismissed && !composing && candidates.length > 0;
  const activeIndex = Math.min(selected, candidates.length - 1);
  const names = [...members].sort((a, b) => b.length - a.length).map((name) => name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|");
  const addressed = [...text.matchAll(new RegExp(`(^|[^A-Za-z0-9._%+-])@(${names})(?=$|[\\s，。！？、,:;!?）)])`, "giu"))]
    .map((item) => members.find((name) => name === item[2]) ?? members.find((name) => name.toLocaleLowerCase() === item[2].toLocaleLowerCase()));
  const mentionAll = /(^|[^A-Za-z0-9._%+-])@all(?=$|[\s，。！？、,:;!?）)])/iu.test(text);
  const mention = mentionAll ? [] : members.filter((name) => addressed.includes(name));
  const canSend = connected && !posting && text.trim() !== "";

  useLayoutEffect(() => {
    if (nextCursor.current === null) return;
    textarea.current?.focus();
    textarea.current?.setSelectionRange(nextCursor.current, nextCursor.current);
    nextCursor.current = null;
  }, [text]);

  function replace(start: number, end: number, value: string) {
    const position = start + value.length;
    nextCursor.current = position;
    setText(text.slice(0, start) + value + text.slice(end));
    setCursor(position);
    setSelected(0);
    setDismissed(false);
  }

  function choose(name: string) {
    if (match === null) return;
    replace(cursor - match[2].length - 1, textarea.current?.selectionEnd ?? cursor, `@${name} `);
  }

  async function send() {
    if (!canSend) return;
    if (await onSend(draft, mention)) {
      clearSentComposerText(draftKey, "text", draft);
      setDraft("");
      setCursor(0);
      setDismissed(false);
    }
  }

  return <div className={`composer group-composer${quote !== null || text.length > 90 || text.includes("\n") ? " is-expanded" : ""}`}>
    {quote === null ? null : <MessageQuote quote={quote} onRemove={() => setDraft(text)} />}
    {opened ? <div className="group-mention-menu">
      <Text className="group-mention-heading" fz={11} c="dimmed">点名群成员</Text>
      <div role="listbox" aria-label="点名候选" id={listId}>
        {candidates.map(({ value, name, all }, index) => <button key={`${all ? "all" : "member"}:${value}`} type="button" role="option"
          id={`${listId}-${index}`} aria-selected={index === activeIndex} tabIndex={-1}
          onMouseDown={(event) => event.preventDefault()} onClick={() => choose(value)}>
          {all ? <span className="agent-avatar agent-avatar-small" aria-hidden="true"><IconUsersGroup size={16} /></span> : <BotAvatar name={name} className="agent-avatar-small" />}
          <span>{name}</span><span className="group-mention-at">@{value}</span>
        </button>)}
      </div>
    </div> : null}
    <Textarea className="composer-input" ref={textarea} aria-label="群消息" autosize maxRows={7} minRows={quote !== null || text.length > 90 || text.includes("\n") ? 2 : 1}
      aria-autocomplete="list" aria-controls={opened ? listId : undefined}
      aria-activedescendant={opened ? `${listId}-${activeIndex}` : undefined}
      onChange={(event) => {
        setText(event.currentTarget.value);
        setCursor(event.currentTarget.selectionStart);
        setDismissed(false);
        setSelected(0);
      }}
      onSelect={(event) => setCursor(event.currentTarget.selectionStart)}
      onBlur={() => setDismissed(true)}
      onCompositionStart={() => setComposing(true)}
      onCompositionEnd={(event) => {
        setComposing(false);
        setCursor(event.currentTarget.selectionStart);
      }}
      onKeyDown={(event) => {
        if (event.nativeEvent.isComposing || composing) return;
        if (opened && ["ArrowDown", "ArrowUp", "Enter", "Tab", "Escape"].includes(event.key) && !event.shiftKey) {
          event.preventDefault();
          if (event.key === "Escape") setDismissed(true);
          else if (event.key === "ArrowDown") setSelected((activeIndex + 1) % candidates.length);
          else if (event.key === "ArrowUp") setSelected((activeIndex - 1 + candidates.length) % candidates.length);
          else choose(candidates[activeIndex].value);
        } else if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          void send();
        }
      }}
      placeholder={`发送到${groupName}，输入 @ 点名成员…`} variant="unstyled" value={text} />
    <Group className="composer-meta" justify="space-between" wrap="nowrap" gap="xs">
      <Group gap={6} wrap="nowrap" className="group-composer-recipients">
        <ActionIcon aria-label="点名成员" variant="subtle" color="gray" radius="xl" size={27} onClick={() => {
          const start = textarea.current?.selectionStart ?? text.length;
          const end = textarea.current?.selectionEnd ?? start;
          const prefix = start > 0 && /[A-Za-z0-9._%+-]/.test(text[start - 1]) ? " @" : "@";
          replace(start, end, prefix);
        }}><IconAt size={17} /></ActionIcon>
        <Text c="dimmed" fz={12} truncate aria-live="polite">
          {mention.length === 0 ? "群消息 · 点名可选" : `发送给 ${mention.map((name) => `@${name}`).join("、")}`}
        </Text>
      </Group>
      <Button className="composer-send" aria-label="发送到群" disabled={!canSend} loading={posting}
        onClick={() => void send()} rightSection={<IconArrowUp size={13} stroke={1.7} />}>发送</Button>
    </Group>
  </div>;
}
