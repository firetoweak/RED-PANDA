import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { PrivateMessageNotice } from "../src/features/conversation/PrivateMessageNotice";

vi.mock("../src/api/kondenApi", () => ({
  useGetAgentsQuery: () => ({ data: [{ name: "Codex", session_id: "internal-bot" }] }),
}));
vi.mock("../src/features/conversation/ChatPresentation", () => ({
  BotAvatar: () => null, useIdentityColor: () => () => "blue",
}));
vi.mock("../src/features/conversation/MarkdownMessage", () => ({
  MarkdownMessage: ({ content }: { content: string }) => <div>{content}</div>,
}));

it.each([
  ["received", "消息来自"],
  ["sent", "已发消息给"],
] as const)("外部消息（%s）只显示方向与身份，正文从对应三方对话查看", (direction, label) => {
  const message = { key: "external:1", kind: "private_message" as const, direction, threadId: null,
    peer: "Codex", occurredAt: "2026-10-23T01:00:00Z",
    text: "来讨论一下方案", messageId: "1", replyTo: null,
    external: { identity: "1".repeat(32) } };
  render(<MemoryRouter initialEntries={["/sessions/receiver"]}><Routes><Route path="/sessions/:sessionId" element={<PrivateMessageNotice agentName="接收方" communication={{ key: message.key, message, messages: [message] }} />} /></Routes></MemoryRouter>);
  expect(screen.getByRole("img", { name: label })).toBeInTheDocument();
  expect(screen.queryByText("外部协作者")).not.toBeInTheDocument();
  expect(screen.queryByText(message.text)).not.toBeInTheDocument();
  expect(screen.getByRole("link", { name: `${label} Codex，查看三方对话` }))
    .toHaveAttribute("href", `/external/${"1".repeat(32)}?bot=receiver`);
});
