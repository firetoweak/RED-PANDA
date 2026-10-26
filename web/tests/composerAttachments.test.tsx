import { MantineProvider } from "@mantine/core";
import { configureStore } from "@reduxjs/toolkit";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Provider } from "react-redux";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { Composer } from "../src/features/conversation/Composer";
import { ChatPresentation } from "../src/features/conversation/ChatPresentation";
import runtimeReducer from "../src/realtime/runtimeSlice";

const { upload, selectLocal } = vi.hoisted(() => ({
  upload: vi.fn(),
  selectLocal: vi.fn(),
}));
vi.mock("../src/api/kondenApi", () => ({
  useGetSessionModelQuery: () => ({ data: {
    selected: { model: "deepseek/test", compact_threshold_tokens: 200000 }, effective: null, pending: true,
  } }),
  useGetModelSettingsQuery: () => ({ data: {
    config: { model: { default: "deepseek/test", candidates: [{ model: "deepseek/test", compact_threshold_tokens: 200000 }] } },
    providers: [{ provider: "deepseek", configured: true, missing: null, local: false }],
  } }),
  useSetSessionModelMutation: () => [vi.fn(), { isLoading: false }],
  useGetWorkspacesQuery: () => ({ data: [] }),
  useUploadAttachmentMutation: () => [upload],
  useSelectLocalFileMutation: () => [selectLocal],
}));

beforeEach(() => {
  localStorage.clear();
  upload.mockReset();
  selectLocal.mockReset();
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  Object.defineProperty(document, "fonts", { configurable: true, value: { addEventListener() {}, removeEventListener() {} } });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function showComposer(running = false, inputTokens: number | null = null, sessionId = "session") {
  const store = configureStore({ reducer: { runtime: runtimeReducer } });
  const onSend = vi.fn(async () => {});
  const props = {
    sessionId, workspaceId: "workspace-test", connectionId: "connection",
    disabled: false, sending: false, running,
    stopBusy: false,
    inputTokens,
    onSend, onStop: vi.fn(),
  };
  const ui = (active: boolean) => <Provider store={store}><MantineProvider>
    <ChatPresentation value={{ names: ["Ada", "Ada QA", "李雷", "C++"], quote: null, onQuote: () => {}, onJump: () => {} }}>
      <Composer {...props} running={active} />
    </ChatPresentation>
  </MantineProvider></Provider>;
  const view = render(ui(running));
  return { ...view, onSend, stop: () => view.rerender(ui(false)) };
}

it("私聊从名册筛选并用完整名字补全，Enter 先选候选再发送", async () => {
  const { onSend } = showComposer();
  const input = screen.getByLabelText("消息");
  fireEvent.change(input, { target: { value: "问问 @aD" } });
  expect(screen.getAllByRole("option")).toHaveLength(2);
  fireEvent.keyDown(input, { key: "ArrowDown" });
  fireEvent.keyDown(input, { key: "Enter" });
  expect(input).toHaveValue("问问 @Ada QA ");
  expect(onSend).not.toHaveBeenCalled();
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  fireEvent.keyDown(input, { key: "Enter" });
  await waitFor(() => expect(onSend).toHaveBeenCalledWith("问问 @Ada QA", []));
});

it("私聊点名支持中文输入法、鼠标和光标处替换，保留后文与引用", () => {
  localStorage.setItem("konden.composerDraft.session", JSON.stringify({ text: "> [Ada](#message:original)\n> @C++ 的原话\n\n" }));
  const { onSend } = showComposer();
  const input = screen.getByLabelText("消息") as HTMLTextAreaElement;
  fireEvent.change(input, { target: { value: "请@李 看一下" } });
  input.setSelectionRange(3, 3);
  fireEvent.select(input);
  fireEvent.compositionStart(input);
  fireEvent.keyDown(input, { key: "Enter" });
  expect(onSend).not.toHaveBeenCalled();
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  fireEvent.compositionEnd(input);
  fireEvent.click(screen.getByRole("option", { name: /李雷/ }));
  expect(input).toHaveValue("请@李雷  看一下");
  expect(input.selectionStart).toBe(5);
  expect(screen.getByRole("button", { name: "取消引用" })).toBeInTheDocument();
});

it("私聊不提供全体候选，邮箱不触发，Escape 关闭后仍可正常发送", async () => {
  const { onSend } = showComposer();
  const input = screen.getByLabelText("消息");
  fireEvent.change(input, { target: { value: "dev@Ada" } });
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  fireEvent.change(input, { target: { value: "@" } });
  expect(screen.getAllByRole("option")).toHaveLength(4);
  expect(screen.queryByRole("option", { name: /所有人/ })).not.toBeInTheDocument();
  fireEvent.keyDown(input, { key: "ArrowUp" });
  fireEvent.keyDown(input, { key: "Tab" });
  expect(input).toHaveValue("@C++ ");
  fireEvent.change(input, { target: { value: "@Ada" } });
  fireEvent.keyDown(input, { key: "Escape" });
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
  expect(onSend).not.toHaveBeenCalled();
  fireEvent.keyDown(input, { key: "Enter" });
  await waitFor(() => expect(onSend).toHaveBeenCalledWith("@Ada", []));
});

it("拖入普通文件后保留名称并以附件引用发送，不要求填写正文", async () => {
  upload.mockImplementation(({ file }: { file: File }) => ({ unwrap: async () => ({
    kind: "file", attachment_id: `file:${file.name === "报告.pdf" ? "a".repeat(32) : "b".repeat(32)}`,
  }) }));
  const { container, onSend } = showComposer();
  const pdf = new File(["pdf"], "报告.pdf", { type: "application/pdf" });
  const doc = new File(["doc"], "report.docx");
  fireEvent.drop(container.querySelector("form")!, { dataTransfer: { files: [pdf, doc] } });
  await waitFor(() => expect(screen.getByRole("button", { name: "发送" })).toBeEnabled());
  expect(screen.getByText("报告.pdf")).toBeVisible();
  expect(screen.getByText("report.docx")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() => expect(onSend).toHaveBeenCalledWith("[File #1] [File #2]", [`file:${"a".repeat(32)}`, `file:${"b".repeat(32)}`]));
});

it("上传失败保留文件并阻止缺附件发送，重试成功后才可提交", async () => {
  upload.mockReturnValueOnce({ unwrap: async () => { throw { data: { detail: "无法写入附件" } }; } })
    .mockReturnValueOnce({ unwrap: async () => ({ kind: "file", attachment_id: `file:${"a".repeat(32)}` }) });
  const { container, onSend } = showComposer();
  fireEvent.change(screen.getByLabelText("消息"), { target: { value: "分析文件" } });
  fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files: [new File(["x"], "notes.txt")] } });
  await screen.findByText("无法写入附件");
  expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
  expect(onSend).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "发送" })).toBeEnabled());
  expect(upload).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() => expect(onSend).toHaveBeenCalledWith("分析文件 [File #1]", [`file:${"a".repeat(32)}`]));
});

it("运行中仍用发送立即提交文字和附件，停止保持为独立操作", async () => {
  upload.mockReturnValue({ unwrap: async () => ({ kind: "file", attachment_id: `file:${"a".repeat(32)}` }) });
  const { container, onSend, stop } = showComposer(true);
  fireEvent.change(screen.getByLabelText("消息"), { target: { value: "改为分析这个附件" } });
  fireEvent.paste(screen.getByLabelText("消息"), { clipboardData: { files: [new File([], "empty.txt")] } });
  await waitFor(() => expect(screen.getByRole("button", { name: "发送", exact: true })).toBeEnabled());
  expect(screen.getByRole("button", { name: "停止", exact: true })).toBeEnabled();
  fireEvent.click(screen.getByRole("button", { name: "发送", exact: true }));
  await waitFor(() => expect(onSend).toHaveBeenCalledWith("改为分析这个附件 [File #1]", [`file:${"a".repeat(32)}`]));
  expect(screen.queryByRole("button", { name: "立即发送" })).not.toBeInTheDocument();
  expect(container.querySelector('input[type="file"]')).not.toHaveAttribute("accept");
  stop();
  expect(onSend).toHaveBeenCalledTimes(1);
});

it("本机选择直接返回已挂入附件，浏览器不传服务器路径", async () => {
  selectLocal.mockReturnValue({ unwrap: async () => ({
    file: { kind: "file", attachment_id: `file:${"a".repeat(32)}`, name: "a.log", size: 12 },
  }) });
  const { onSend } = showComposer();
  fireEvent.click(screen.getByRole("button", { name: "Add attachment" }));
  await waitFor(() => expect(screen.getByText("a.log")).toBeVisible());
  expect(upload).not.toHaveBeenCalled();
  expect(selectLocal).toHaveBeenCalledWith({
    connectionId: "connection", sessionId: "session",
  });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() => expect(onSend).toHaveBeenCalledWith("[File #1]", [`file:${"a".repeat(32)}`]));
});

it("本机选择失败可重新选择，取消会移除失败项并恢复发送", async () => {
  selectLocal.mockReturnValueOnce({ unwrap: async () => { throw { data: { detail: "无法打开本机选择窗口" } }; } })
    .mockReturnValueOnce({ unwrap: async () => ({ file: null }) });
  const { onSend } = showComposer();
  fireEvent.change(screen.getByLabelText("消息"), { target: { value: "hello" } });
  fireEvent.click(screen.getByRole("button", { name: "Add attachment" }));
  await screen.findByText("无法打开本机选择窗口");
  expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "发送" })).toBeEnabled());
  expect(selectLocal).toHaveBeenCalledTimes(2);
  expect(screen.queryByText("本机文件")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() => expect(onSend).toHaveBeenCalledWith("hello", []));
});

it("没有实时用量推送时也显示会话返回的历史用量", () => {
  showComposer(false, 1500);
  expect(screen.getByRole("img", { name: /1% 已用.*1.5K \/ 200K tokens/ })).toBeVisible();
  expect(screen.queryByRole("img", { name: /暂无本窗口用量/ })).not.toBeInTheDocument();
});

it("没有本窗口调用记录时提示未知用量而不是零", () => {
  showComposer();
  expect(screen.getByRole("img", { name: /暂无本窗口用量/ })).toBeVisible();
  expect(screen.queryByRole("img", { name: /0% 已用/ })).not.toBeInTheDocument();
});

it("切换会话和重新打开页面后恢复各自的文字草稿，清空也会保存", () => {
  const first = showComposer();
  fireEvent.change(screen.getByLabelText("消息"), { target: { value: "第一条草稿\n继续写" } });
  first.unmount();
  const second = showComposer(false, null, "other-session");
  expect(screen.getByLabelText("消息")).toHaveValue("");
  fireEvent.change(screen.getByLabelText("消息"), { target: { value: "另一条草稿" } });
  second.unmount();
  const restored = showComposer();
  expect(screen.getByLabelText("消息")).toHaveValue("第一条草稿\n继续写");
  fireEvent.change(screen.getByLabelText("消息"), { target: { value: "" } });
  restored.unmount();
  showComposer();
  expect(screen.getByLabelText("消息")).toHaveValue("");
});

it("发送等待确认或失败时保留草稿，确认成功才清除", async () => {
  const first = showComposer();
  let reject!: (error: Error) => void;
  first.onSend.mockImplementationOnce(() => new Promise<void>((_, fail) => { reject = fail; }));
  fireEvent.change(screen.getByLabelText("消息"), { target: { value: "需要保存" } });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  first.unmount();
  const retry = showComposer();
  expect(screen.getByLabelText("消息")).toHaveValue("需要保存");
  reject(new Error("offline"));
  await Promise.resolve();
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() => expect(screen.getByLabelText("消息")).toHaveValue(""));
  retry.unmount();
  showComposer();
  expect(screen.getByLabelText("消息")).toHaveValue("");
});

it("发送确认后不覆盖用户在等待期间新写的文字", async () => {
  const view = showComposer();
  let resolve!: () => void;
  view.onSend.mockImplementationOnce(() => new Promise<void>((done) => { resolve = done; }));
  fireEvent.change(screen.getByLabelText("消息"), { target: { value: "已提交" } });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  fireEvent.change(screen.getByLabelText("消息"), { target: { value: "下一条草稿" } });
  resolve();
  await waitFor(() => expect(screen.getByLabelText("消息")).toHaveValue("下一条草稿"));
  view.unmount();
  showComposer();
  expect(screen.getByLabelText("消息")).toHaveValue("下一条草稿");
});

it("运行中发送失败后保留文字草稿，刷新后也不自动发送", async () => {
  const view = showComposer(true);
  view.onSend.mockRejectedValueOnce(new Error("发送失败"));
  fireEvent.change(screen.getByLabelText("消息"), { target: { value: "改为执行新要求" } });
  fireEvent.click(screen.getByRole("button", { name: "发送", exact: true }));
  await waitFor(() => expect(view.onSend).toHaveBeenCalledOnce());
  expect(screen.getByLabelText("消息")).toHaveValue("改为执行新要求");
  view.unmount();
  const restored = showComposer(true);
  expect(screen.getByLabelText("消息")).toHaveValue("改为执行新要求");
  restored.stop();
  expect(restored.onSend).not.toHaveBeenCalled();
});
