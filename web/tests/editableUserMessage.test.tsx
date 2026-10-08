import { MantineProvider } from "@mantine/core";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { EditableUserMessage } from "../src/features/conversation/EditableUserMessage";

const writeText = vi.fn();

beforeEach(() => {
  writeText.mockReset().mockResolvedValue(undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText }, userAgent: navigator.userAgent });
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  Object.defineProperty(document, "fonts", { configurable: true, value: { addEventListener() {}, removeEventListener() {} } });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  Reflect.deleteProperty(document, "fonts");
});

it("消息下方呈现发送时间，复制原文不提交编辑，断线仍能复制", async () => {
  const onSave = vi.fn();
  render(<MantineProvider><EditableUserMessage
    sessionId="session" text="本项目还未提交的代码，都有哪些成分"
    occurredAt="2026-10-03T08:00:05Z" images={[]} files={[]}
    disabled saving={false} hasLaterWork onSave={onSave}
  /></MantineProvider>);

  const copy = screen.getByRole("button", { name: "复制消息" });
  const edit = screen.getByRole("button", { name: "编辑消息" });
  const footer = copy.closest(".user-message-actions")!;
  expect(footer).toContainElement(edit);
  expect(footer.querySelector("time")).toHaveAttribute("datetime", "2026-10-03T08:00:05Z");
  expect(footer.querySelector("time")).toHaveAttribute("title", expect.stringContaining("发送于"));
  expect(edit).toBeDisabled();
  fireEvent.click(copy);
  await screen.findByRole("button", { name: "已复制消息" });
  expect(writeText).toHaveBeenCalledWith("本项目还未提交的代码，都有哪些成分");
  expect(onSave).not.toHaveBeenCalled();
});

it("移动后的编辑按钮仍能修改并提交消息", async () => {
  const onSave = vi.fn().mockResolvedValue(undefined);
  render(<MantineProvider><EditableUserMessage
    sessionId="session" text="原始消息" occurredAt="2026-10-03T08:00:05Z"
    images={[]} files={[]} disabled={false} saving={false} hasLaterWork={false} onSave={onSave}
  /></MantineProvider>);
  fireEvent.click(screen.getByRole("button", { name: "编辑消息" }));
  fireEvent.change(screen.getByRole("textbox", { name: "编辑消息" }), { target: { value: "修改后的消息" } });
  fireEvent.click(screen.getByRole("button", { name: "改写并执行" }));
  await waitFor(() => expect(onSave).toHaveBeenCalledWith("修改后的消息", false, []));
  await screen.findByText("原始消息");
});

it("编辑时隐藏标记、展示可移除附件，提交只携带留下的附件并重新编号", async () => {
  const onSave = vi.fn().mockResolvedValue(undefined);
  render(<MantineProvider><EditableUserMessage
    sessionId="session" text="查看这些材料 [Image #1] [Image #2] [File #1]"
    occurredAt="2026-10-03T08:00:05Z" images={["image-a", "image-b"]}
    files={[{ attachment_id: "file-a", name: "报告.txt", size: 8 }]}
    disabled={false} saving={false} hasLaterWork={false} onSave={onSave}
  /></MantineProvider>);
  fireEvent.click(screen.getByRole("button", { name: "编辑消息" }));
  expect(screen.getByRole("textbox", { name: "编辑消息" })).toHaveValue("查看这些材料");
  fireEvent.click(screen.getAllByRole("button", { name: "Remove 图片" })[0]);
  fireEvent.click(screen.getByRole("button", { name: "Remove 报告.txt" }));
  expect(screen.getAllByRole("button", { name: "Remove 图片" })).toHaveLength(1);
  expect(screen.queryByRole("button", { name: "Remove 报告.txt" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "改写并执行" }));
  await waitFor(() => expect(onSave).toHaveBeenCalledWith("查看这些材料 [Image #1]", false, ["image-b"]));
});

it("纯附件消息能重发，移除最后一个附件后不能发送空消息，取消恢复原附件", () => {
  render(<MantineProvider><EditableUserMessage
    sessionId="session" text="[Image #1]" occurredAt="2026-10-03T08:00:05Z"
    images={["image-a"]} files={[]} disabled={false} saving={false} hasLaterWork={false} onSave={vi.fn()}
  /></MantineProvider>);
  fireEvent.click(screen.getByRole("button", { name: "编辑消息" }));
  expect(screen.getByRole("textbox", { name: "编辑消息" })).toHaveValue("");
  expect(screen.getByRole("button", { name: "改写并执行" })).toBeEnabled();
  fireEvent.click(screen.getByRole("button", { name: "Remove 图片" }));
  expect(screen.getByRole("button", { name: "改写并执行" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "取消编辑" }));
  fireEvent.click(screen.getByRole("button", { name: "编辑消息" }));
  expect(screen.getByRole("button", { name: "Remove 图片" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "改写并执行" })).toBeEnabled();
});

it("点击编辑区域外取消正文和附件修改，区域内的附件、预览与回退选项保持编辑", async () => {
  const onSave = vi.fn();
  render(<MantineProvider><EditableUserMessage
    sessionId="session" text="原始消息 [Image #1]" occurredAt="2026-10-03T08:00:05Z"
    images={["image-a"]} files={[]} disabled={false} saving={false} hasLaterWork onSave={onSave}
  /></MantineProvider>);
  fireEvent.click(screen.getByRole("button", { name: "编辑消息" }));
  const input = screen.getByRole("textbox", { name: "编辑消息" });
  fireEvent.pointerDown(input);
  fireEvent.change(input, { target: { value: "临时修改" } });
  const image = screen.getByRole("button", { name: "图片" });
  fireEvent.pointerDown(image);
  fireEvent.click(image);
  const dialog = await screen.findByRole("dialog");
  fireEvent.pointerDown(dialog);
  expect(input).toBeInTheDocument();
  fireEvent.keyDown(dialog, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  const remove = screen.getByRole("button", { name: "Remove 图片" });
  fireEvent.pointerDown(remove);
  fireEvent.click(remove);
  expect(input).toHaveValue("临时修改");

  fireEvent.pointerDown(document.body);
  expect(screen.queryByRole("textbox", { name: "编辑消息" })).not.toBeInTheDocument();
  expect(onSave).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "编辑消息" }));
  expect(screen.getByRole("textbox", { name: "编辑消息" })).toHaveValue("原始消息");
  expect(screen.getByRole("button", { name: "Remove 图片" })).toBeInTheDocument();
});

it("这条消息之后还有工作时，发送才问文件退不退", async () => {
  const onSave = vi.fn().mockResolvedValue(undefined);
  render(<MantineProvider><EditableUserMessage
    sessionId="session" text="原始消息" occurredAt="2026-10-03T08:00:05Z"
    images={[]} files={[]} disabled={false} saving={false} hasLaterWork onSave={onSave}
  /></MantineProvider>);
  fireEvent.click(screen.getByRole("button", { name: "编辑消息" }));
  fireEvent.change(screen.getByRole("textbox", { name: "编辑消息" }), { target: { value: "修改后的消息" } });
  fireEvent.click(screen.getByRole("button", { name: "改写并执行" }));
  expect(onSave).not.toHaveBeenCalled();
  fireEvent.click(await screen.findByRole("button", { name: "只回滚会话" }));
  await waitFor(() => expect(onSave).toHaveBeenCalledWith("修改后的消息", false, []));

  onSave.mockClear();
  fireEvent.click(screen.getByRole("button", { name: "编辑消息" }));
  fireEvent.click(screen.getByRole("button", { name: "改写并执行" }));
  fireEvent.click(await screen.findByRole("button", { name: "会话和文件一起回滚" }));
  await waitFor(() => expect(onSave).toHaveBeenCalledWith("原始消息", true, []));
});

it("提交进行中点击外部不取消编辑", () => {
  const props = {
    sessionId: "session", text: "原始消息", occurredAt: "2026-10-03T08:00:05Z",
    images: [], files: [], disabled: false, hasLaterWork: false, onSave: vi.fn(),
  };
  const view = render(<MantineProvider><EditableUserMessage {...props} saving={false} /></MantineProvider>);
  fireEvent.click(screen.getByRole("button", { name: "编辑消息" }));
  view.rerender(<MantineProvider><EditableUserMessage {...props} saving /></MantineProvider>);
  fireEvent.pointerDown(document.body);
  expect(screen.getByRole("textbox", { name: "编辑消息" })).toBeInTheDocument();
});
