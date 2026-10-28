import { MantineProvider } from "@mantine/core";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { ExternalConversation } from "../src/api/contracts";
import { ExternalSessionPage } from "../src/features/external/ExternalSessionPage";

const query = vi.hoisted(() => ({ data: undefined as ExternalConversation | undefined, connectionId: "human" as string | null,
  save: vi.fn(), retry: vi.fn() }));
vi.mock("../src/api/kondenApi", () => ({
  useGetExternalConversationQuery: () => ({ data: query.data }),
  useSetExternalCodexMutation: () => [query.save, { isLoading: false }],
  useRetryExternalCodexMutation: () => [query.retry, { isLoading: false }],
}));
vi.mock("../src/app/hooks", () => ({ useAppSelector: () => query.connectionId }));
vi.mock("../src/features/conversation/MarkdownMessage", () => ({ MarkdownMessage: ({ content }: { content: string }) => <p>{content}</p> }));

const identity = "1".repeat(32);
const codex = { thread_id: null, executable: "codex", status: "disabled", detail: null, attempted_at: null } as const;
const session = { identity, name: "外部会话-11111111", active: true,
  created_at: "2026-10-25T01:00:00Z", last_activity_at: "2026-10-25T01:00:00Z", peers: [] };
beforeEach(() => {
  query.connectionId = "human";
  query.save.mockReset().mockReturnValue({ unwrap: () => Promise.resolve() });
  query.retry.mockReset().mockReturnValue({ unwrap: () => Promise.resolve() });
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
  query.data = { session: { ...session, active: false }, codex, messages: [] };
  open();
  expect(screen.getByText("此会话尚无消息")).toBeVisible();
  expect(screen.getByText("接入已撤销，已有对话记录继续保留。")).toBeVisible();
  expect(screen.getByRole("region", { name: "外部会话消息" })).toBeVisible();
});

it("按 Bot 查看完整往返，并可切回全部对话", async () => {
  query.data = { session: { ...session, peers: [
    { name: "墨竹", session_id: "alpha", deleted: false }, { name: "松风", session_id: "beta", deleted: false },
  ] }, codex, messages: [
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

it("用户为当前外部会话绑定 Codex 聊天，保存前校验聊天 ID", async () => {
  query.data = { session, codex, messages: [] };
  open();
  fireEvent.click(screen.getByRole("button", { name: "Codex 自动接续" }));
  fireEvent.change(screen.getByRole("textbox", { name: /Codex 聊天 ID/ }), { target: { value: "wrong-chat" } });
  fireEvent.click(screen.getByRole("button", { name: "保存并启用" }));
  expect(screen.getByRole("alert")).toHaveTextContent("请填写 Codex 聊天 ID");
  expect(query.save).not.toHaveBeenCalled();
  fireEvent.change(screen.getByRole("textbox", { name: /Codex 聊天 ID/ }), { target: { value: "01234567-89ab-cdef-0123-456789abcdef" } });
  fireEvent.click(screen.getByRole("button", { name: "保存并启用" }));
  expect(query.save).toHaveBeenCalledWith({ connectionId: "human", identity,
    target: { thread_id: "01234567-89ab-cdef-0123-456789abcdef", executable: "codex" } });
  await waitFor(() => expect(screen.getByRole("button", { name: "Codex 自动接续" })).toHaveAttribute("aria-expanded", "false"));
});

it("提醒结果未确认时可重试，断线后停用和保存都不可用", async () => {
  query.data = { session, codex: { ...codex, thread_id: "01234567-89ab-cdef-0123-456789abcdef",
    status: "unknown", detail: "等待 Codex 响应超时，无法确认提示是否已入队。" }, messages: [] };
  query.retry.mockReturnValue({ unwrap: () => Promise.reject("原聊天不可用，请打开后重试。") });
  open();
  expect(screen.getByRole("status", { name: "Codex 自动接续状态" })).toHaveTextContent("提醒结果未确认");
  fireEvent.click(screen.getByRole("button", { name: "重试提醒" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("原聊天不可用");
  expect(query.retry).toHaveBeenCalledWith({ connectionId: "human", identity });
  query.connectionId = null;
  fireEvent.click(screen.getByRole("button", { name: "Codex 自动接续" }));
  expect(screen.getByRole("button", { name: "停用自动接续" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "保存设置" })).toBeDisabled();
});
