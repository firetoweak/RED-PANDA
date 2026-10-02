import { MantineProvider } from "@mantine/core";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { ModelSettingsPage } from "../src/features/models/ModelSettingsPage";
import { ModelTestButton } from "../src/features/models/ModelTestButton";

const { testModel, settings } = vi.hoisted(() => ({
  testModel: vi.fn(),
  settings: {
    config: { model: { default: "deepseek/pro", candidates: [
      { model: "deepseek/pro", compact_threshold_tokens: 200000 },
      { model: "deepseek/flash", compact_threshold_tokens: 240000 },
    ] } },
    connections_path: "personal/connections.json",
    providers: [{ provider: "deepseek", configured: true, missing: null, local: false }],
  },
}));
vi.mock("../src/app/hooks", () => ({ useAppSelector: () => "connection" }));
vi.mock("../src/api/helpermeApi", () => ({
  useGetModelSettingsQuery: () => ({ data: settings }),
  useSaveModelSettingsMutation: () => [vi.fn(), { isLoading: false }],
  useTestModelMutation: () => [testModel, { isLoading: false }],
}));

beforeEach(() => {
  testModel.mockReset();
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("测试按钮调用所在行的模型，显示失败原因并允许重新测试", async () => {
  testModel.mockReturnValueOnce({ unwrap: async () => ({
    model: "deepseek/flash", ok: false, message: "当前密钥无权访问此模型", elapsed_ms: 100,
  }) }).mockReturnValueOnce({ unwrap: async () => ({
    model: "deepseek/flash", ok: true, message: "模型可用", elapsed_ms: 80,
  }) });
  render(<MantineProvider><ModelSettingsPage /></MantineProvider>);
  const button = await screen.findByRole("button", { name: "测试 deepseek/flash" });
  fireEvent.click(button);
  expect(await screen.findByText(/当前密钥无权访问此模型/)).toBeVisible();
  expect(testModel).toHaveBeenCalledWith({ connectionId: "connection", model: "deepseek/flash" });
  expect(screen.getByRole("radio", { name: "默认模型 deepseek/pro" })).toBeChecked();
  fireEvent.click(button);
  expect(await screen.findByText(/测试通过 · 80 ms/)).toBeVisible();
  expect(screen.queryByText(/当前密钥无权访问此模型/)).not.toBeInTheDocument();
  expect(testModel).toHaveBeenCalledTimes(2);
});

it("尚未保存的模型提示先保存且不能发起测试", () => {
  render(<MantineProvider><ModelTestButton model="deepseek/new" connectionId="connection"
    saved={false} configured /></MantineProvider>);
  const button = screen.getByRole("button", { name: "测试 deepseek/new" });
  expect(button).toBeDisabled();
  expect(button).toHaveAttribute("title", "请先保存模型");
  fireEvent.click(button);
  expect(testModel).not.toHaveBeenCalled();
});
