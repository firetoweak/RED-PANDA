import { MantineProvider } from "@mantine/core";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { ExternalConversation, SessionModel } from "../src/api/contracts";
import { ExternalSessionPage } from "../src/features/external/ExternalSessionPage";

const query = vi.hoisted(() => ({ data: undefined as ExternalConversation | undefined, connectionId: "human" as string | null,
  agents: [] as { session_id: string; name: string }[], save: vi.fn(), retry: vi.fn(), post: vi.fn(), upload: vi.fn(),
  models: {} as Record<string, SessionModel>, usage: {} as Record<string, number | null>, modelSelect: vi.fn() }));
vi.mock("../src/api/kondenApi", async (importOriginal) => ({
  apiErrorDetail: (await importOriginal<typeof import("../src/api/kondenApi")>()).apiErrorDetail,
  useGetConversationQuery: (id: string) => ({ currentData: query.usage[id] === undefined ? undefined : { context_input_tokens: query.usage[id] } }),
  useGetSessionModelQuery: (id: string) => ({ data: query.models[id], currentData: query.models[id] }),
  useGetModelCatalogQuery: () => ({ currentData: [] }),
  useGetModelSettingsQuery: () => ({ data: {
    config: { model: { default: "deepseek/test", candidates: [
      { model: "deepseek/test", compact_threshold_tokens: 200000 }, { model: "deepseek/other", compact_threshold_tokens: 100000 },
    ] } }, providers: [{ provider: "deepseek", configured: true }],
  } }),
  useSetSessionModelMutation: () => [query.modelSelect, { isLoading: false }],
  useGetExternalConversationQuery: () => ({ data: query.data }),
  useGetAgentsQuery: () => ({ data: query.agents }),
  usePostExternalMessageMutation: () => [query.post, { isLoading: false }],
  useUploadAttachmentMutation: () => [query.upload],
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
  query.models = {
    alpha: { selected: { model: "deepseek/test", reasoning_effort: null, compact_threshold_tokens: 200000 }, effective: null, pending: false },
    beta: { selected: { model: "deepseek/other", reasoning_effort: null, compact_threshold_tokens: 100000 }, effective: null, pending: false },
  };
  query.usage = { alpha: 20000, beta: null };
  query.modelSelect.mockReset().mockReturnValue({ unwrap: () => Promise.resolve() });
  query.save.mockReset().mockReturnValue({ unwrap: () => Promise.resolve() });
  query.retry.mockReset().mockReturnValue({ unwrap: () => Promise.resolve() });
  query.post.mockReset().mockReturnValue({ unwrap: () => Promise.resolve() });
  query.upload.mockReset().mockImplementation(({ file }: { file: File }) => ({ unwrap: () => Promise.resolve({
    kind: "file", attachment_id: "file:" + "a".repeat(32), name: file.name, size: file.size,
  }) }));
  URL.createObjectURL = vi.fn(() => "blob:test-preview");
  URL.revokeObjectURL = vi.fn();
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
    { message_id: "m1", sequence: 1, attachments: [], direction: "received", sender: "codex", recipients: ["bot"], text: "讨论需求", bot_name: "墨竹", bot_session_id: "alpha", reply_to: null, occurred_at: session.created_at },
    { message_id: "m2", sequence: 2, attachments: [], direction: "sent", sender: "bot", recipients: ["codex"], text: "第一位的回复", bot_name: "墨竹", bot_session_id: "alpha", reply_to: "m1", occurred_at: session.created_at },
    { message_id: "m3", sequence: 3, attachments: [], direction: "sent", sender: "bot", recipients: ["codex"], text: "第二位的主动消息", bot_name: "松风", bot_session_id: "beta", reply_to: null, occurred_at: session.created_at },
  ] };
  open("?bot=alpha");
  expect(screen.getByRole("button", { name: "模型设置：DeepSeek test" })).toBeVisible();
  expect(screen.getByRole("img", { name: "10% 已用 · 20K / 200K tokens" })).toBeVisible();
  const messages = within(screen.getByRole("region", { name: "外部会话消息" }));
  expect(messages.getByText("第一位的回复")).toBeVisible();
  expect(messages.queryByText("第二位的主动消息")).not.toBeInTheDocument();
  expect(messages.getByRole("button", { name: "查看 外部会话-11111111 的原消息" })).toBeVisible();
  expect(messages.getAllByRole("link", { name: "墨竹" })[0]).toHaveAttribute("href", "/sessions/alpha");
  fireEvent.change(screen.getByRole("textbox", { name: "三方会话消息" }), { target: { value: "给墨竹的草稿" } });
  fireEvent.click(screen.getByRole("combobox", { name: "三方对话中的 Bot" }));
  expect(screen.queryByRole("option", { name: "全部 Bot 的对话" })).not.toBeInTheDocument();
  fireEvent.click(await screen.findByRole("option", { name: "松风" }));
  expect(screen.getByRole("button", { name: "模型设置：DeepSeek other" })).toBeVisible();
  expect(screen.getByRole("img", { name: "暂无本窗口用量 · — / 100K tokens" })).toBeVisible();
  expect(screen.queryByRole("img", { name: "10% 已用 · 20K / 200K tokens" })).not.toBeInTheDocument();
  expect(screen.getByText("第二位的主动消息")).toBeVisible();
  expect(screen.queryByText("第一位的回复")).not.toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "三方会话消息" })).toHaveValue("");
  fireEvent.click(screen.getByRole("combobox", { name: "三方对话中的 Bot" }));
  fireEvent.click(await screen.findByRole("option", { name: "墨竹" }));
  expect(screen.getByRole("img", { name: "10% 已用 · 20K / 200K tokens" })).toBeVisible();
  expect(screen.getByRole("textbox", { name: "三方会话消息" })).toHaveValue("给墨竹的草稿");
});

it("三方页切换所选 Bot 的模型，确认前禁止发送且保留草稿", async () => {
  query.data = { session: { ...session, peers: [{ name: "墨竹", session_id: "alpha", deleted: false }] }, codex, messages: [] };
  let finish!: () => void;
  query.modelSelect.mockReturnValueOnce({ unwrap: () => new Promise<void>((resolve) => { finish = resolve; }) });
  open("?bot=alpha");
  fireEvent.change(screen.getByRole("textbox", { name: "三方会话消息" }), { target: { value: "使用新模型回复" } });
  fireEvent.click(screen.getByRole("button", { name: "模型设置：DeepSeek test" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: "切换模型：DeepSeek test" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: "DeepSeek other", exact: true }));
  expect(query.modelSelect).toHaveBeenCalledWith({ sessionId: "alpha", connectionId: "human", model: "deepseek/other", reasoningEffort: null });
  expect(screen.getByRole("button", { name: "发送三方消息" })).toBeDisabled();
  fireEvent.keyDown(screen.getByRole("textbox", { name: "三方会话消息" }), { key: "Enter" });
  expect(query.post).not.toHaveBeenCalled();
  expect(screen.getByRole("textbox", { name: "三方会话消息" })).toHaveValue("使用新模型回复");
  await act(async () => { finish(); });
  fireEvent.click(screen.getByRole("button", { name: "发送三方消息" }));
  await waitFor(() => expect(query.post).toHaveBeenCalledWith(expect.objectContaining({ botSessionId: "alpha", text: "使用新模型回复" })));
});

it("无历史时先选择 Bot，三方来源和接收方可区分", async () => {
  query.agents = [{ session_id: "alpha", name: "墨竹" }, { session_id: "beta", name: "松风" }];
  query.data = { session, codex, messages: [] };
  open();
  expect(screen.queryByRole("textbox", { name: "三方会话消息" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("combobox", { name: "三方对话中的 Bot" }));
  fireEvent.click(await screen.findByRole("option", { name: "墨竹" }));
  expect(screen.getByText("三方对话已准备好")).toBeVisible();
  expect(screen.queryByRole("combobox", { name: "发送给谁" })).not.toBeInTheDocument();
  fireEvent.change(screen.getByRole("textbox", { name: "三方会话消息" }), { target: { value: "@all 三方都能看见" } });
  expect(screen.getByText(/发送给 @墨竹、@Codex/)).toHaveTextContent("Codex 需主动收信");
  fireEvent.click(screen.getByRole("button", { name: "发送三方消息" }));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "三方会话消息" })).toHaveValue(""));
  expect(query.post).toHaveBeenCalledWith({ connectionId: "human", identity, botSessionId: "alpha", text: "@all 三方都能看见",
    target: "both", deliveryId: expect.stringMatching(/^web-/), replyTo: null, artifactRefs: [] });
});

it("发送失败保留引用、正文和编号，重试成功后清空", async () => {
  query.data = { session: { ...session, peers: [{ name: "墨竹", session_id: "alpha", deleted: false }] }, codex, messages: [
    { message_id: "human", sequence: 1, attachments: [], direction: "user_bot", sender: "user", recipients: ["bot"], text: "人的原话", bot_name: "墨竹", bot_session_id: "alpha", reply_to: null, occurred_at: session.created_at },
    { message_id: "bot", sequence: 2, attachments: [], direction: "bot_user", sender: "bot", recipients: ["user"], text: "@Codex 给人的回复", bot_name: "墨竹", bot_session_id: "alpha", reply_to: "human", occurred_at: session.created_at },
  ] };
  query.post.mockReturnValueOnce({ unwrap: () => Promise.reject("暂时断开，请重试") });
  open("?bot=alpha");
  expect(screen.getByRole("article", { name: "你的消息" })).toHaveTextContent("你 → 墨竹");
  expect(screen.getByRole("article", { name: "墨竹的消息" })).toHaveTextContent("墨竹 → 你");
  fireEvent.contextMenu(screen.getByText("@Codex 给人的回复"));
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

it("用 @ 补全接收方，确认候选的 Enter 不直接发送，删除点名后恢复默认 Bot", async () => {
  query.data = { session: { ...session, peers: [{ name: "墨竹", session_id: "alpha", deleted: false }] }, codex, messages: [] };
  open("?bot=alpha");
  const input = screen.getByRole("textbox", { name: "三方会话消息" });
  fireEvent.click(screen.getByRole("button", { name: "点名接收方" }));
  expect(input).toHaveValue("@");
  expect(screen.getByRole("option", { name: /双方.*@all/ })).toBeVisible();
  fireEvent.keyDown(input, { key: "ArrowUp" });
  fireEvent.keyDown(input, { key: "Enter" });
  expect(input).toHaveValue("@Codex ");
  expect(query.post).not.toHaveBeenCalled();
  expect(screen.getByText(/发送给 @Codex/)).toBeVisible();
  fireEvent.change(input, { target: { value: "普通发言" } });
  expect(screen.getByText("默认给 @墨竹")).toBeVisible();
  fireEvent.keyDown(input, { key: "Enter" });
  await waitFor(() => expect(query.post).toHaveBeenCalledWith(expect.objectContaining({ text: "普通发言", target: "bot" })));
});

it.each([
  ["@codex，请确认", "codex"], ["@墨竹 @Codex 请共同确认", "both"],
  ["dev@Codex.example 和 @CodexTest 都只是正文", "bot"],
])("只按正文中的完整点名发送：%s", async (text, target) => {
  query.data = { session: { ...session, peers: [{ name: "墨竹", session_id: "alpha", deleted: false }] }, codex, messages: [] };
  open("?bot=alpha");
  fireEvent.change(screen.getByRole("textbox", { name: "三方会话消息" }), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "发送三方消息" }));
  await waitFor(() => expect(query.post).toHaveBeenCalledWith(expect.objectContaining({ text, target })));
});

it("Bot 也叫 Codex 时点名仍能区分两个来源", async () => {
  query.data = { session: { ...session, peers: [{ name: "Codex", session_id: "alpha", deleted: false }] }, codex, messages: [] };
  open("?bot=alpha");
  const input = screen.getByRole("textbox", { name: "三方会话消息" });
  fireEvent.change(input, { target: { value: "@Codex 只请原生聊天处理" } });
  fireEvent.click(screen.getByRole("button", { name: "发送三方消息" }));
  await waitFor(() => expect(query.post).toHaveBeenCalledWith(expect.objectContaining({ target: "codex" })));
  fireEvent.change(input, { target: { value: "@Codex（Bot） 只请 Bot 处理" } });
  fireEvent.click(screen.getByRole("button", { name: "发送三方消息" }));
  await waitFor(() => expect(query.post).toHaveBeenLastCalledWith(expect.objectContaining({ target: "bot" })));
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

it("普通附件可单独发送，文件名不点名；发送失败后沿原附件和编号重试", async () => {
  query.data = { session: { ...session, peers: [{ name: "墨竹", session_id: "alpha", deleted: false }] }, codex, messages: [] };
  let uploaded!: (value: unknown) => void;
  query.upload.mockReturnValueOnce({ unwrap: () => new Promise((resolve) => { uploaded = resolve; }) });
  query.post.mockReturnValueOnce({ unwrap: () => Promise.reject("网络断开，请重试") });
  open("?bot=alpha");
  const picker = screen.getByLabelText("选择三方附件");
  const clicked = vi.spyOn(picker, "click");
  fireEvent.click(screen.getByRole("button", { name: "添加附件" }));
  expect(clicked).toHaveBeenCalledOnce();
  const file = new File(["zip bytes"], "@Codex 报告.zip", { type: "application/zip" });
  fireEvent.change(picker, { target: { files: [file] } });
  expect(screen.getByRole("button", { name: "发送三方消息" })).toBeDisabled();
  uploaded({ kind: "file", attachment_id: "file:" + "a".repeat(32), name: file.name, size: file.size });
  await waitFor(() => expect(screen.getByRole("button", { name: "发送三方消息" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "发送三方消息" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("网络断开");
  expect(screen.getByText("@Codex 报告.zip")).toBeVisible();
  const first = query.post.mock.calls[0][0];
  expect(first).toMatchObject({ target: "bot", text: "[File #1]", artifactRefs: ["file:" + "a".repeat(32)] });
  cleanup();
  open("?bot=alpha");
  expect(screen.getByText("@Codex 报告.zip")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "发送三方消息" }));
  await waitFor(() => expect(screen.queryByText("@Codex 报告.zip")).not.toBeInTheDocument());
  expect(query.post.mock.calls[1][0]).toEqual(first);
  expect(query.upload).toHaveBeenCalledOnce();
});

it("粘贴图片失败可重试，未完成上传时不能发送，移除后释放预览", async () => {
  query.data = { session: { ...session, peers: [{ name: "墨竹", session_id: "alpha", deleted: false }] }, codex, messages: [] };
  query.upload.mockReturnValueOnce({ unwrap: () => Promise.reject({ data: { detail: "图片格式无效" } }) });
  open("?bot=alpha");
  const input = screen.getByRole("textbox", { name: "三方会话消息" });
  fireEvent.paste(input, { clipboardData: { files: [new File(["image"], "截图.png", { type: "image/png" })] } });
  const retry = await screen.findByRole("button", { name: "重试" });
  expect(retry).toHaveAttribute("title", "图片格式无效");
  expect(screen.getByRole("button", { name: "发送三方消息" })).toBeDisabled();
  query.upload.mockReturnValueOnce({ unwrap: () => Promise.resolve({ kind: "image", attachment_id: "sha256:" + "b".repeat(64),
    name: "截图.png", size: 5, mime: "image/png", width: 32, height: 24 }) });
  fireEvent.click(retry);
  await waitFor(() => expect(screen.getByRole("button", { name: "发送三方消息" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Remove 截图.png" }));
  expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:test-preview");
  expect(screen.getByRole("button", { name: "发送三方消息" })).toBeDisabled();
});

it("三种来源的附件都有预览或下载入口，附件占位符不作为正文展示", async () => {
  const image = { kind: "image" as const, attachment_id: "sha256:" + "b".repeat(64), name: "图片", size: 5,
    mime: "image/png" as const, width: 32, height: 24 };
  const file = { kind: "file" as const, attachment_id: "file:" + "a".repeat(32), name: "报告.xlsx", size: 2048 };
  query.data = { session: { ...session, peers: [{ name: "墨竹", session_id: "alpha", deleted: false }] }, codex, messages: [
    { message_id: "img", sequence: 1, direction: "received", sender: "codex", recipients: ["bot"], text: "看图", attachments: [image],
      bot_name: "墨竹", bot_session_id: "alpha", reply_to: null, occurred_at: session.created_at },
    { message_id: "file", sequence: 2, direction: "sent", sender: "bot", recipients: ["codex"], text: "报告", attachments: [file],
      bot_name: "墨竹", bot_session_id: "alpha", reply_to: null, occurred_at: session.created_at },
    { message_id: "user-file", sequence: 3, direction: "user_bot", sender: "user", recipients: ["bot"], text: "[File #1]", attachments: [{ ...file, name: "材料.zip" }],
      bot_name: "墨竹", bot_session_id: "alpha", reply_to: null, occurred_at: session.created_at },
  ] };
  open("?bot=alpha");
  expect(screen.getByRole("link", { name: "报告.xlsx" })).toHaveAttribute("href", "/api/sessions/alpha/attachments/" + encodeURIComponent(file.attachment_id));
  expect(screen.getByRole("link", { name: "材料.zip" })).toBeVisible();
  expect(screen.queryByText("[File #1]")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "图片" }));
  expect(await screen.findByRole("dialog", { name: "图片" })).toBeVisible();
  expect(screen.getByRole("link", { name: "下载原图" })).toHaveAttribute("href", "/api/sessions/alpha/attachments/" + encodeURIComponent(image.attachment_id));
});
