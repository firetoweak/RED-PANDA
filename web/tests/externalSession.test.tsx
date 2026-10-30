import { MantineProvider } from "@mantine/core";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { ExternalConversation } from "../src/api/contracts";
import { ExternalSessionPage } from "../src/features/external/ExternalSessionPage";

const query = vi.hoisted(() => ({ data: undefined as ExternalConversation | undefined, connectionId: "human" as string | null,
  agents: [] as { session_id: string; name: string }[], save: vi.fn(), retry: vi.fn(), post: vi.fn() }));
vi.mock("../src/api/kondenApi", () => ({
  useGetExternalConversationQuery: () => ({ data: query.data }),
  useGetAgentsQuery: () => ({ data: query.agents }),
  usePostExternalMessageMutation: () => [query.post, { isLoading: false }],
  useSetExternalCodexMutation: () => [query.save, { isLoading: false }],
  useRetryExternalCodexMutation: () => [query.retry, { isLoading: false }],
}));
vi.mock("../src/app/hooks", () => ({ useAppSelector: () => query.connectionId }));
vi.mock("../src/features/conversation/MarkdownMessage", () => ({ MarkdownMessage: ({ content }: { content: string }) => <p>{content}</p> }));

const identity = "1".repeat(32);
const codex = { thread_id: "11111111-1111-1111-1111-111111111111", executable: "codex", status: "disabled", detail: null, attempted_at: null } as const;
const session = { identity, name: "外部会话-11111111", active: true, codex_status: "disabled" as const,
  created_at: "2026-10-25T01:00:00Z", last_activity_at: "2026-10-25T01:00:00Z", peers: [] };
beforeEach(() => {
  localStorage.clear();
  query.agents = [];
  query.connectionId = "human";
  query.save.mockReset().mockReturnValue({ unwrap: () => Promise.resolve() });
  query.retry.mockReset().mockReturnValue({ unwrap: () => Promise.resolve() });
  query.post.mockReset().mockReturnValue({ unwrap: () => Promise.resolve() });
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

it("每段三方对话固定一位 Bot，消息和草稿按 Bot 分开", async () => {
  query.data = { session: { ...session, peers: [
    { name: "墨竹", session_id: "alpha", deleted: false }, { name: "松风", session_id: "beta", deleted: false },
  ] }, codex, messages: [
    { message_id: "m1", sequence: 1, direction: "received", sender: "codex", recipients: ["bot"], text: "讨论需求", bot_name: "墨竹", bot_session_id: "alpha", reply_to: null, occurred_at: session.created_at },
    { message_id: "m2", sequence: 2, direction: "sent", sender: "bot", recipients: ["codex"], text: "第一位的回复", bot_name: "墨竹", bot_session_id: "alpha", reply_to: "m1", occurred_at: session.created_at },
    { message_id: "m3", sequence: 3, direction: "sent", sender: "bot", recipients: ["codex"], text: "第二位的主动消息", bot_name: "松风", bot_session_id: "beta", reply_to: null, occurred_at: session.created_at },
  ] };
  open("?bot=alpha");
  const messages = within(screen.getByRole("region", { name: "外部会话消息" }));
  expect(messages.getByText("第一位的回复")).toBeVisible();
  expect(messages.queryByText("第二位的主动消息")).not.toBeInTheDocument();
  expect(messages.getByRole("button", { name: "查看 外部会话-11111111 的原消息" })).toBeVisible();
  expect(messages.getAllByRole("link", { name: "墨竹" })[0]).toHaveAttribute("href", "/sessions/alpha");
  fireEvent.change(screen.getByRole("textbox", { name: "三方会话消息" }), { target: { value: "给墨竹的草稿" } });
  fireEvent.click(screen.getByRole("combobox", { name: "三方对话中的 Bot" }));
  expect(screen.queryByRole("option", { name: "全部 Bot 的对话" })).not.toBeInTheDocument();
  fireEvent.click(await screen.findByRole("option", { name: "松风" }));
  expect(screen.getByText("第二位的主动消息")).toBeVisible();
  expect(screen.queryByText("第一位的回复")).not.toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "三方会话消息" })).toHaveValue("");
  fireEvent.click(screen.getByRole("combobox", { name: "三方对话中的 Bot" }));
  fireEvent.click(await screen.findByRole("option", { name: "墨竹" }));
  expect(screen.getByRole("textbox", { name: "三方会话消息" })).toHaveValue("给墨竹的草稿");
});

it("无历史时先选择 Bot，三方来源和接收方可区分", async () => {
  query.agents = [{ session_id: "alpha", name: "墨竹" }, { session_id: "beta", name: "松风" }];
  query.data = { session, codex, messages: [] };
  open();
  expect(screen.queryByRole("textbox", { name: "三方会话消息" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("combobox", { name: "三方对话中的 Bot" }));
  fireEvent.click(await screen.findByRole("option", { name: "墨竹" }));
  expect(screen.getByText("三方对话已准备好")).toBeVisible();
  fireEvent.change(screen.getByRole("textbox", { name: "三方会话消息" }), { target: { value: "三方都能看见" } });
  fireEvent.click(screen.getByRole("combobox", { name: "发送给谁" }));
  fireEvent.click(await screen.findByRole("option", { name: "发送给双方" }));
  expect(screen.getByText("Codex 需主动收信")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "发送三方消息" }));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "三方会话消息" })).toHaveValue(""));
  expect(query.post).toHaveBeenCalledWith({ connectionId: "human", identity, botSessionId: "alpha", text: "三方都能看见",
    target: "both", deliveryId: expect.stringMatching(/^web-/), replyTo: null });
});

it("发送失败保留引用、正文和编号，重试成功后清空", async () => {
  query.data = { session: { ...session, peers: [{ name: "墨竹", session_id: "alpha", deleted: false }] }, codex, messages: [
    { message_id: "human", sequence: 1, direction: "user_bot", sender: "user", recipients: ["bot"], text: "人的原话", bot_name: "墨竹", bot_session_id: "alpha", reply_to: null, occurred_at: session.created_at },
    { message_id: "bot", sequence: 2, direction: "bot_user", sender: "bot", recipients: ["user"], text: "Bot 给人的回复", bot_name: "墨竹", bot_session_id: "alpha", reply_to: "human", occurred_at: session.created_at },
  ] };
  query.post.mockReturnValueOnce({ unwrap: () => Promise.reject("暂时断开，请重试") });
  open("?bot=alpha");
  expect(screen.getByRole("article", { name: "你的消息" })).toHaveTextContent("你 → 墨竹");
  expect(screen.getByRole("article", { name: "墨竹的消息" })).toHaveTextContent("墨竹 → 你");
  fireEvent.contextMenu(screen.getByText("Bot 给人的回复"));
  fireEvent.click(await screen.findByRole("menuitem", { name: "引用 墨竹 的消息" }));
  fireEvent.change(screen.getByRole("textbox", { name: "三方会话消息" }), { target: { value: "请继续" } });
  fireEvent.click(screen.getByRole("button", { name: "发送三方消息" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("暂时断开，请重试");
  expect(screen.getByRole("textbox", { name: "三方会话消息" })).toHaveValue("请继续");
  expect(screen.getByRole("button", { name: "取消引用" })).toBeVisible();
  const first = query.post.mock.calls[0][0];
  expect(first).toMatchObject({ target: "bot", text: "请继续", replyTo: "bot" });
  fireEvent.click(screen.getByRole("button", { name: "发送三方消息" }));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "三方会话消息" })).toHaveValue(""));
  expect(query.post.mock.calls[1][0]).toEqual(first);
  expect(screen.queryByRole("button", { name: "取消引用" })).not.toBeInTheDocument();
});

it.each(["disconnected", "revoked", "deleted"])("%s 时保留草稿而禁止发送", (unavailable) => {
  query.connectionId = unavailable === "disconnected" ? null : "human";
  query.data = { session: { ...session, active: unavailable !== "revoked", peers: [{ name: "墨竹", session_id: "alpha", deleted: unavailable === "deleted" }] }, codex, messages: [] };
  localStorage.setItem(`konden.composerDraft.external:${identity}:alpha`, JSON.stringify({ text: "尚未发出" }));
  open("?bot=alpha");
  expect(screen.getByRole("textbox", { name: "三方会话消息" })).toHaveValue("尚未发出");
  expect(screen.getByRole("textbox", { name: "三方会话消息" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "发送三方消息" })).toBeDisabled();
  expect(query.post).not.toHaveBeenCalled();
});

it("当前聊天自动定位，用户只需确认程序并启用接续", async () => {
  query.data = { session, codex, messages: [] };
  open();
  fireEvent.click(screen.getByRole("button", { name: "Codex 自动接续" }));
  expect(screen.queryByRole("textbox", { name: /Codex 聊天 ID/ })).not.toBeInTheDocument();
  expect(screen.getByText(`已自动识别原生聊天：${codex.thread_id}`)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "保存并启用" }));
  expect(query.save).toHaveBeenCalledWith({ connectionId: "human", identity,
    target: { executable: "codex" } });
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
