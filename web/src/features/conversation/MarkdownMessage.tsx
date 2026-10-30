import { createRemendPreprocessor } from "@ai-markdown/react";
import MantineAIMarkdown from "@ai-markdown/react-mantine";
import hljs from "highlight.js/lib/common";
import { Children, cloneElement, isValidElement, memo, useContext, type ComponentProps, type ReactNode } from "react";
import { ChatPresentation, useIdentityColor, MessageQuote, splitMessageQuote } from "./ChatPresentation";
import { AttachmentTile } from "./AttachmentTile";

const highlightedCodeBlock = {
  autoDetectUnknownLanguage: true,
  highlightJs: hljs,
} as const;
const contentPreprocessors = [createRemendPreprocessor()];

function MentionText({ children }: { children: ReactNode }) {
  const { names } = useContext(ChatPresentation);
  const identityColor = useIdentityColor();
  if (names.length === 0) return children;
  const alternatives = [...names].sort((a, b) => b.length - a.length)
    .map((name) => name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|");
  function decorate(nodes: ReactNode): ReactNode {
    return Children.map(nodes, (node) => {
      if (typeof node === "string") {
        const expression = new RegExp(`(^|[^A-Za-z0-9._%+-])@(${alternatives})(?=$|[\\s，。！？、,:;!?）)])`, "gu");
        const parts: ReactNode[] = [];
        let offset = 0;
        for (const match of node.matchAll(expression)) {
          const start = match.index + match[1].length;
          parts.push(node.slice(offset, start), <span key={start} className="message-mention" data-identity={identityColor(match[2])}>@{match[2]}</span>);
          offset = start + match[2].length + 1;
        }
        parts.push(node.slice(offset));
        return parts;
      }
      if (!isValidElement<{ children?: ReactNode; node?: { tagName?: string } }>(node)) return node;
      if (["pre", "code", "a"].includes(String(node.type)) || ["pre", "code", "a"].includes(node.props.node?.tagName ?? "")) return node;
      return cloneElement(node, { children: decorate(node.props.children) });
    });
  }
  return decorate(children);
}
function MentionParagraph({ children, node: _node, ...props }: ComponentProps<"p"> & { node?: unknown }) {
  return <p {...props}><MentionText>{children}</MentionText></p>;
}
function MentionListItem({ children, node: _node, ...props }: ComponentProps<"li"> & { node?: unknown }) {
  return <li {...props}><MentionText>{children}</MentionText></li>;
}
function MessageImage({src, alt}: ComponentProps<"img">) {
  return typeof src === "string" ? <AttachmentTile src={src} name={alt || "图片"} large downloadHref={src} /> : null;
}
const customComponents = { p: MentionParagraph, li: MentionListItem, img: MessageImage };

interface MarkdownMessageProps {
  content: string;
  streaming: boolean;
}

export const MarkdownMessage = memo(function MarkdownMessage({
  content,
  streaming,
}: MarkdownMessageProps) {
  const { quote, body } = splitMessageQuote(content);
  return (
    <div className="markdown-message" data-streaming={streaming || undefined}>
      {quote === null ? null : <MessageQuote quote={quote} />}
      <MantineAIMarkdown
        codeBlock={streaming ? undefined : highlightedCodeBlock}
        content={body}
        customComponents={customComponents}
        contentPreprocessors={contentPreprocessors}
        fontSize="inherit"
        streaming={streaming}
      />
    </div>
  );
});
