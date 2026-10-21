import { ActionIcon, Tooltip } from "@mantine/core";
import { IconArrowBackUp, IconX } from "@tabler/icons-react";
import { createContext, useContext } from "react";

export type MessageReference = { id: string; author: string; preview: string };
export const ChatPresentation = createContext<{
  names: string[];
  quote: MessageReference | null;
  onQuote: (message: MessageReference | null) => void;
  onJump: (id: string) => void;
}>({ names: [], quote: null, onQuote: () => {}, onJump: () => {} });

/** 名字决定身份色；头像、作者和正文点名共用这一映射。 */
export function identityColor(name: string): string {
  const approved: Record<string, string> = { Atlas: "blue", Nova: "purple", Echo: "green", Mori: "amber" };
  if (Object.hasOwn(approved, name)) return approved[name];
  let hash = 0;
  for (const char of name) hash = (hash * 31 + char.codePointAt(0)!) | 0;
  return ["blue", "purple", "green", "amber"][(hash >>> 0) % 4];
}

export function BotAvatar({ name, className = "" }: { name: string; className?: string }) {
  return <span className={`agent-avatar ${className}`} data-identity={identityColor(name)} aria-hidden="true">{name.slice(0, 1)}</span>;
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
  return <div className="message-quote">
    <button type="button" onClick={() => onJump(quote.id)} aria-label={`查看 ${quote.author} 的原消息`}>
      <strong data-identity={identityColor(quote.author)}>{quote.author}</strong>
      <span>{quote.preview}</span>
    </button>
    {onRemove === undefined ? null : <ActionIcon variant="subtle" color="gray" size="sm" aria-label="取消引用" onClick={onRemove}><IconX size={14} /></ActionIcon>}
  </div>;
}

export function QuoteAction({ message }: { message: MessageReference }) {
  const { onQuote } = useContext(ChatPresentation);
  return <Tooltip label="引用消息"><ActionIcon aria-label={`引用 ${message.author} 的消息`} variant="subtle" color="gray" size="sm" onClick={() => onQuote(message)}><IconArrowBackUp size={15} /></ActionIcon></Tooltip>;
}
