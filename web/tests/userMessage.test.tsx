import { MantineProvider } from "@mantine/core";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { UserMessage } from "../src/features/conversation/UserMessage";

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

it("消息下方呈现发送时间，复制原文", async () => {
  render(<MantineProvider><UserMessage
    sessionId="session" text="本项目还未提交的代码，都有哪些成分"
    occurredAt="2026-10-03T08:00:05Z" images={[]} files={[]}
  /></MantineProvider>);

  const copy = screen.getByRole("button", { name: "复制消息" });
  const footer = copy.closest(".user-message-actions")!;
  expect(screen.queryByRole("button", { name: "编辑消息" })).not.toBeInTheDocument();
  expect(footer.querySelector("time")).toHaveAttribute("datetime", "2026-10-03T08:00:05Z");
  expect(footer.querySelector("time")).toHaveAttribute("title", expect.stringContaining("发送于"));
  fireEvent.click(copy);
  await screen.findByRole("button", { name: "已复制消息" });
  expect(writeText).toHaveBeenCalledWith("本项目还未提交的代码，都有哪些成分");
});
