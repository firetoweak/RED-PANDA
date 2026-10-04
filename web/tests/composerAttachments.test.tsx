import { MantineProvider } from "@mantine/core";
import { configureStore } from "@reduxjs/toolkit";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Provider } from "react-redux";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { Composer } from "../src/features/conversation/Composer";
import runtimeReducer from "../src/realtime/runtimeSlice";

const { upload, selectLocal, attachLocal } = vi.hoisted(() => ({
  upload: vi.fn(),
  selectLocal: vi.fn(),
  attachLocal: vi.fn(),
}));
vi.mock("../src/api/redpandaApi", () => ({
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
  useAttachLocalFileMutation: () => [attachLocal],
}));

beforeEach(() => {
  upload.mockReset();
  selectLocal.mockReset();
  attachLocal.mockReset();
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  Object.defineProperty(document, "fonts", { configurable: true, value: { addEventListener() {}, removeEventListener() {} } });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function showComposer(running = false, inputTokens: number | null = null) {
  const store = configureStore({ reducer: { runtime: runtimeReducer } });
  const onSend = vi.fn(async () => {});
  const props = {
    sessionId: "session", workspaceId: "workspace-test", connectionId: "connection",
    disabled: false, sending: false, running, paused: false, shouldWake: false,
    pauseBusy: false, retryBusy: false, autoAuthorize: false, autoAuthorizeBusy: false,
    inputTokens,
    onSend, onSetPaused: vi.fn(), onRetry: vi.fn(), onToggleAutoAuthorize: vi.fn(),
  };
  const ui = (active: boolean) => <Provider store={store}><MantineProvider><Composer {...props} running={active} /></MantineProvider></Provider>;
  const view = render(ui(running));
  return { ...view, onSend, stop: () => view.rerender(ui(false)) };
}

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

it("运行中停放的文件在当前轮结束后一起发送", async () => {
  upload.mockReturnValue({ unwrap: async () => ({ kind: "file", attachment_id: `file:${"a".repeat(32)}` }) });
  const { container, onSend, stop } = showComposer(true);
  fireEvent.paste(screen.getByLabelText("消息"), { clipboardData: { files: [new File([], "empty.txt")] } });
  await waitFor(() => expect(screen.getByRole("button", { name: "发送" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  expect(onSend).not.toHaveBeenCalled();
  expect(screen.getByText("附件（1）")).toBeVisible();
  expect(container.querySelector('input[type="file"]')).not.toHaveAttribute("accept");
  stop();
  await waitFor(() => expect(onSend).toHaveBeenCalledWith("[File #1]", [`file:${"a".repeat(32)}`]));
});

it("挂入本机文件只登记路径，不走整件上传", async () => {
  selectLocal.mockReturnValue({ unwrap: async () => ({
    file: { path: "D:\\logs\\a.log", name: "a.log" },
  }) });
  attachLocal.mockReturnValue({ unwrap: async () => ({
    kind: "file", attachment_id: `file:${"a".repeat(32)}`, name: "a.log", size: 12,
  }) });
  const { onSend } = showComposer();
  fireEvent.click(screen.getByRole("button", { name: "Add attachment" }));
  await waitFor(() => expect(screen.getByText("a.log")).toBeVisible());
  expect(upload).not.toHaveBeenCalled();
  expect(attachLocal).toHaveBeenCalledWith({
    connectionId: "connection", sessionId: "session", path: "D:\\logs\\a.log",
  });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() => expect(onSend).toHaveBeenCalledWith("[File #1]", [`file:${"a".repeat(32)}`]));
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
