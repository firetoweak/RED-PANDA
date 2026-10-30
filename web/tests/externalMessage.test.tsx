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

it("普通外部消息展示正文，并跳转到外部会话而不是同名内部 Bot", () => {
  const message = { key: "external:1", kind: "private_message" as const, direction: "received" as const, threadId: null,
    peer: "Codex", occurredAt: "2026-10-23T01:00:00Z",
    text: "来讨论一下方案", messageId: "1", replyTo: null,
    external: { identity: "1".repeat(32) } };
  render(<MemoryRouter initialEntries={["/sessions/receiver"]}><Routes><Route path="/sessions/:sessionId" element={<PrivateMessageNotice agentName="接收方" communication={{ key: message.key, message, messages: [message] }} />} /></Routes></MemoryRouter>);
  expect(screen.getByText("外部协作者")).toBeInTheDocument();
  expect(screen.getByText("来讨论一下方案")).toBeInTheDocument();
  expect(screen.getByRole("link")).toHaveAttribute("href", `/external/${"1".repeat(32)}?bot=receiver`);
});
