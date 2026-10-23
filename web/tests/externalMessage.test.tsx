import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { PrivateMessageNotice } from "../src/features/conversation/PrivateMessageNotice";

vi.mock("../src/api/redpandaApi", () => ({
  useGetAgentsQuery: () => ({ data: [{ name: "Codex", session_id: "internal-bot" }] }),
}));
vi.mock("../src/features/conversation/ChatPresentation", () => ({
  BotAvatar: () => null, useIdentityColor: () => () => "blue",
}));
vi.mock("../src/features/conversation/MarkdownMessage", () => ({
  MarkdownMessage: ({ content }: { content: string }) => <div>{content}</div>,
}));

it("外部消息展示来源、正文和版本，不跳转到同名内部 Bot", () => {
  render(<PrivateMessageNotice message={{ key: "external:1", kind: "private_message", direction: "received",
    peer: "Codex", occurredAt: "2026-10-23T01:00:00Z",
    external: { text: "请测试这个版本", revision: "a".repeat(40), reference: "PR 1" } }} />);
  expect(screen.getByText("外部协作者")).toBeInTheDocument();
  expect(screen.getByText("请测试这个版本")).toBeInTheDocument();
  expect(screen.getByText("a".repeat(40))).toBeInTheDocument();
  expect(screen.queryByRole("link")).not.toBeInTheDocument();
});
