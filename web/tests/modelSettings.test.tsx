import { MantineProvider } from "@mantine/core";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { ModelSettings } from "../src/features/models/ModelSettings";
import { ModelTestButton } from "../src/features/models/ModelTestButton";
import type { CodexModels } from "../src/api/contracts";

const { testModel, settings, catalog, saveSettings } = vi.hoisted(() => ({
  testModel: vi.fn(),
  saveSettings: vi.fn(),
  catalog: { currentData: undefined as CodexModels | undefined, error: undefined as unknown, isFetching: false, refetch: vi.fn() },
  settings: {
    config: { model: { default: "deepseek/pro", candidates: [
      { model: "deepseek/pro", compact_threshold_tokens: 200000 },
      { model: "deepseek/flash", compact_threshold_tokens: 240000 },
    ] } },
    connections_path: "personal/connections.json",
    providers: [{ provider: "deepseek", configured: true, missing: null, local: false },
                { provider: "openai-codex", configured: true, missing: null, local: false }],
  },
}));
vi.mock("../src/app/hooks", () => ({ useAppSelector: () => "connection" }));
vi.mock("../src/api/redpandaApi", () => ({
  useGetModelSettingsQuery: () => ({ data: settings }),
  useSaveModelSettingsMutation: () => [saveSettings, { isLoading: false }],
  useGetCodexModelsQuery: () => catalog,
  useTestModelMutation: () => [testModel, { isLoading: false }],
}));

beforeEach(() => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  testModel.mockReset();
  saveSettings.mockReset().mockImplementation(({ config }) => ({ unwrap: async () => ({ ...settings, config }) }));
  catalog.error = undefined; catalog.isFetching = false; catalog.refetch.mockReset();
  catalog.currentData = [{ slug: "ExactCase-ID", display_name: "Example model", context_window: 500000,
    default_reasoning_level: "low", supported_reasoning_levels: [{ effort: "low" }, { effort: "high" }] },
    { slug: "second-id", display_name: "Another model", context_window: 272000,
      default_reasoning_level: "medium", supported_reasoning_levels: [{ effort: "medium" }, { effort: "xhigh" }] }];
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
  render(<MantineProvider><ModelSettings /></MantineProvider>);
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

async function openCodex() {
  fireEvent.click(screen.getByRole("button", { name: "添加模型", exact: true }));
  fireEvent.click(await screen.findByRole("combobox", { name: "供应商" }));
  fireEvent.click(await screen.findByRole("option", { name: "openai-codex", exact: true }));
}

it("Codex 从账号目录选择，展示能力并保存原始 ID，compact 独立设置", async () => {
  render(<MantineProvider env="test"><ModelSettings /></MantineProvider>);
  await openCodex();
  const select = screen.getByRole("combobox", { name: "模型名称" });
  expect(screen.getByRole("button", { name: "加入待保存配置" })).toBeDisabled();
  fireEvent.click(select);
  fireEvent.click(await screen.findByRole("option", { name: "Example model (ExactCase-ID)", exact: true }));
  expect(screen.getByText("上下文容量：500,000 tokens")).toBeVisible();
  expect(screen.getByText("支持的推理档位：low / high")).toBeVisible();
  fireEvent.click(select);
  fireEvent.click(await screen.findByRole("option", { name: "Another model (second-id)", exact: true }));
  expect(screen.getByText("上下文容量：272,000 tokens")).toBeVisible();
  expect(screen.getByText("支持的推理档位：medium / xhigh")).toBeVisible();
  expect(screen.queryByText("上下文容量：500,000 tokens")).not.toBeInTheDocument();
  fireEvent.click(select);
  fireEvent.click(await screen.findByRole("option", { name: "Example model (ExactCase-ID)", exact: true }));
  fireEvent.click(screen.getByRole("button", { name: "加入待保存配置" }));
  fireEvent.click(screen.getByRole("button", { name: "保存配置", exact: true }));
  expect(saveSettings).toHaveBeenCalledWith(expect.objectContaining({ config: { model: {
    ...settings.config.model, candidates: [...settings.config.model.candidates,
      { model: "openai-codex/ExactCase-ID", compact_threshold_tokens: 200000 }],
  } } }));
});

it("读取目录期间不使用旧结果，失败可重试，空目录不能加入", async () => {
  catalog.isFetching = true;
  const view = render(<MantineProvider env="test"><ModelSettings /></MantineProvider>);
  await openCodex();
  expect(screen.getByRole("status")).toHaveTextContent("正在读取当前账号的模型列表");
  expect(screen.getByRole("combobox", { name: "模型名称" })).toBeDisabled();
  catalog.isFetching = false; catalog.error = "请重新授权";
  view.rerender(<MantineProvider env="test"><ModelSettings /></MantineProvider>);
  expect(screen.getByText("请重新授权")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
  expect(catalog.refetch).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("button", { name: "加入待保存配置" })).toBeDisabled();
  catalog.error = undefined; catalog.currentData = [];
  view.rerender(<MantineProvider env="test"><ModelSettings /></MantineProvider>);
  expect(screen.getByText("当前账号没有可选择的 Codex 模型。")).toBeVisible();
  expect(screen.getByRole("button", { name: "加入待保存配置" })).toBeDisabled();
});
