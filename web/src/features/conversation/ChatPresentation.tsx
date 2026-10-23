import { ActionIcon, Menu } from "@mantine/core";
import { IconArrowBackUp, IconCopy, IconX } from "@tabler/icons-react";
import { createContext, useContext, type ReactElement } from "react";
import { botAvatarPath, identityHash } from "./botAvatar";

export type MessageReference = { id: string; author: string; preview: string };
export const ChatPresentation = createContext<{
  names: string[];
  quote: MessageReference | null;
  onQuote: (message: MessageReference | null) => void;
  onJump: (id: string) => void;
}>({ names: [], quote: null, onQuote: () => {}, onJump: () => {} });

/** 名册提供当前名字对应的固定身份；历史中无法关联的名字仍可独立显示。 */
export const BotIdentities = createContext<ReadonlyMap<string, string>>(new Map());

function identityColor(seed: string): string {
  return ["blue", "purple", "green", "amber"][identityHash(seed) % 4];
}

export function useIdentityColor() {
  const identities = useContext(BotIdentities);
  return (name: string) => identityColor(identities.get(name) ?? name);
}

export function BotAvatar({ name, sessionId, className = "" }: { name: string; sessionId?: string; className?: string }) {
  const identities = useContext(BotIdentities);
  const seed = sessionId ?? identities.get(name) ?? name;
  return <span className={`agent-avatar ${className}`} data-identity={identityColor(seed)} aria-hidden="true">
    <svg className="bot-avatar-mark" viewBox="0 0 64 64" fill="none" stroke="currentColor" strokeWidth={3} strokeLinecap="round" strokeLinejoin="round" focusable="false">
      <path d={botAvatarPath(seed)} />
    </svg>
  </span>;
}

export function messageReference(id: string, author: string, text: string): MessageReference {
  return { id, author, preview: splitMessageQuote(text).body.replace(/^[#>]+\s*/gm, "").replace(/\[([^\]]+)\]\([^)]*\)/g, "$1").replace(/[`*_]/g, "").replace(/\s+/g, " ").slice(0, 140) };
}

/** 引用作为普通 Markdown 随消息保存，不改变群聊通知或执行语义。 */
export function withMessageQuote(body: string, quote: MessageReference | null): string {
  if (quote === null) return body;
  const author = quote.author.replace(/[\[\]\r\n]/g, "");
  return `> [${author}](#message:${encodeURIComponent(quote.id)})\n> ${quote.preview.replace(/[\r\n]/g, " ")}\n\n${body}`;
}

export function splitMessageQuote(text: string): { quote: MessageReference | null; body: string } {
  const match = text.match(/^> \[([^\]\n]*)\]\(#message:([^\s)]*)\)\n> ([^\n]*)\n\n/u);
  if (match === null) return { quote: null, body: text };
  try {
    return { quote: { id: decodeURIComponent(match[2]), author: match[1], preview: match[3] }, body: text.slice(match[0].length) };
  } catch (error) {
    if (error instanceof URIError) return { quote: null, body: text };
    throw error;
  }
}

export function MessageQuote({ quote, onRemove }: { quote: MessageReference; onRemove?: () => void }) {
  const { onJump } = useContext(ChatPresentation);
  const identityColor = useIdentityColor();
  return <div className="message-quote">
    <button type="button" onClick={() => onJump(quote.id)} aria-label={`查看 ${quote.author} 的原消息`}>
      <strong data-identity={identityColor(quote.author)}>{quote.author}</strong>
      <span>{quote.preview}</span>
    </button>
    {onRemove === undefined ? null : <ActionIcon variant="subtle" color="gray" size="sm" aria-label="取消引用" onClick={onRemove}><IconX size={14} /></ActionIcon>}
  </div>;
}

export function MessageMenu({ id, author, text, children }: {
  id?: string; author: string; text: string; children: ReactElement;
}) {
  const { onQuote } = useContext(ChatPresentation);
  return <Menu withinPortal position="bottom-start" returnFocus>
    <Menu.ContextMenu>{children}</Menu.ContextMenu>
    <Menu.Dropdown>
      <Menu.Item leftSection={<IconArrowBackUp size={15} />} disabled={id === undefined}
        aria-label={`引用 ${author} 的消息`} onClick={() => onQuote(messageReference(id!, author, text))}>引用消息</Menu.Item>
      <Menu.Item leftSection={<IconCopy size={15} />} onClick={() => void navigator.clipboard.writeText(text)}>复制消息</Menu.Item>
    </Menu.Dropdown>
  </Menu>;
}
