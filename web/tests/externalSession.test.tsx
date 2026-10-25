import { MantineProvider } from "@mantine/core";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { ExternalConversation } from "../src/api/contracts";
import { ExternalSessionPage } from "../src/features/external/ExternalSessionPage";

const query = vi.hoisted(() => ({ data: undefined as ExternalConversation | undefined }));
vi.mock("../src/api/kondenApi", () => ({ useGetExternalConversationQuery: () => ({ data: query.data }) }));
vi.mock("../src/features/conversation/MarkdownMessage", () => ({ MarkdownMessage: ({ content }: { content: string }) => <p>{content}</p> }));

const identity = "1".repeat(32);
const session = { identity, name: "外部会话-11111111", active: true,
  created_at: "2026-10-25T01:00:00Z", last_activity_at: "2026-10-25T01:00:00Z", peers: [] };
beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} unobserve() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  Object.defineProperty(document, "fonts", { configurable: true, value: new EventTarget() });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); Reflect.deleteProperty(document, "fonts"); });
function open(search = "") {
  return render(<MantineProvider env="test"><MemoryRouter initialEntries={[`/external/${identity}${search}`]}>
    <Routes><Route path="/external/:identity" element={<ExternalSessionPage />} /></Routes>
  </MemoryRouter></MantineProvider>);
}

it("会话登记后即有可理解的空状态，撤销后仍可查看", () => {
  query.data = { session: { ...session, active: false }, messages: [] };
  open();
  expect(screen.getByText("此会话尚无消息")).toBeVisible();
  expect(screen.getByText("接入已撤销，已有对话记录继续保留。")).toBeVisible();
  expect(screen.getByRole("region", { name: "外部会话消息" })).toBeVisible();
});

it("按 Bot 查看完整往返，并可切回全部对话", async () => {
  query.data = { session: { ...session, peers: [
    { name: "墨竹", session_id: "alpha", deleted: false }, { name: "松风", session_id: "beta", deleted: false },
  ] }, messages: [
    { message_id: "m1", sequence: 1, direction: "received", text: "讨论需求", bot_name: "墨竹", bot_session_id: "alpha", reply_to: null, occurred_at: session.created_at },
    { message_id: "m2", sequence: 2, direction: "sent", text: "第一位的回复", bot_name: "墨竹", bot_session_id: "alpha", reply_to: "m1", occurred_at: session.created_at },
    { message_id: "m3", sequence: 3, direction: "sent", text: "第二位的主动消息", bot_name: "松风", bot_session_id: "beta", reply_to: null, occurred_at: session.created_at },
  ] };
  open("?bot=alpha");
  const messages = within(screen.getByRole("region", { name: "外部会话消息" }));
  expect(messages.getByText("第一位的回复")).toBeVisible();
  expect(messages.queryByText("第二位的主动消息")).not.toBeInTheDocument();
  expect(messages.getByRole("button", { name: "查看 外部会话-11111111 的原消息" })).toBeVisible();
  expect(messages.getAllByRole("link", { name: "墨竹" })[0]).toHaveAttribute("href", "/sessions/alpha");
  fireEvent.click(screen.getByRole("combobox", { name: "查看与哪位 Bot 的对话" }));
  fireEvent.click(await screen.findByRole("option", { name: "全部 Bot 的对话" }));
  expect(screen.getByText("第二位的主动消息")).toBeVisible();
});
