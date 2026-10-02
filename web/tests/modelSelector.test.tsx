import { MantineProvider } from "@mantine/core";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ModelSelector } from "../src/features/models/ModelSelector";

const { select } = vi.hoisted(() => ({ select: vi.fn() }));
vi.mock("../src/api/helpermeApi", () => ({
  useGetSessionModelQuery: () => ({ data: { selected: { model: "deepseek/pro" } } }),
  useGetModelSettingsQuery: () => ({ data: {
    config: { model: { candidates: [{ model: "deepseek/pro" }, { model: "deepseek/flash" }] } },
    providers: [{ provider: "deepseek", configured: true }],
  } }),
  useSetSessionModelMutation: () => [select, { isLoading: false }],
}));

beforeEach(() => {
  select.mockReset().mockReturnValue({ unwrap: async () => ({}) });
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("模型按钮只显示模型名，点击展开列表，切换仍提交完整模型标识", async () => {
  render(<MantineProvider><ModelSelector sessionId="session" connectionId="connection" /></MantineProvider>);
  const button = screen.getByRole("button", { name: "切换会话模型：pro" });
  expect(button).toHaveTextContent("pro");
  expect(button).not.toHaveTextContent("deepseek/");
  expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  fireEvent.mouseEnter(button);
  expect(screen.queryByRole("menuitem", { name: "flash" })).not.toBeInTheDocument();
  fireEvent.click(button);
  fireEvent.click(await screen.findByRole("menuitem", { name: "flash" }));
  await waitFor(() => expect(select).toHaveBeenCalledWith({
    sessionId: "session", connectionId: "connection", model: "deepseek/flash",
  }));
});
