import { MantineProvider } from "@mantine/core";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ModelSettingsDrawer } from "../previews/models/ModelSettingsDrawer";
import { ModelPicker } from "../previews/models/ModelPicker";
import { ModelPreview } from "../previews/models/ModelPreview";
import type { CatalogModel, ModelPreviewApi, ModelTestResult, Preferences } from "../previews/models/api";

const MODELS: CatalogModel[] = [
  { id: "openai/alpha", provider: "openai", name: "Alpha", description: "alpha", contextTokens: 100_000,
    efforts: [{ value: "medium", label: "中" }, { value: "high", label: "高" }], defaultEffort: "medium" },
  { id: "openai/plain", provider: "openai", name: "Plain", description: "plain", contextTokens: 100_000,
    efforts: [], defaultEffort: null },
  { id: "deepseek/beta", provider: "deepseek", name: "Beta", description: "beta", contextTokens: 100_000,
    efforts: [{ value: "low", label: "低" }, { value: "high", label: "高" }, { value: "max", label: "最高" }], defaultEffort: "high" },
];
const PREFERENCES: Preferences = {
  favorites: { "openai/alpha": { effort: null, compactTokens: 70_000 }, "deepseek/beta": { effort: null, compactTokens: 70_000 } },
  defaultModel: "openai/alpha",
};
function fakeApi(): ModelPreviewApi {
  return {
    loadModels: vi.fn(async (provider?: string) => MODELS.filter((item) => !provider || item.provider === provider)),
    loadPreferences: vi.fn(async () => structuredClone(PREFERENCES)),
    savePreferences: vi.fn(async (value) => structuredClone(value)),
    testModel: vi.fn(async () => ({ ok: true as const, latencyMs: 420 })),
    reply: vi.fn(async () => "示例回复已完成"),
  };
}
beforeEach(() => {
  window.history.replaceState(null, "", "/model-preview.html");
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  HTMLElement.prototype.scrollTo = vi.fn();
  HTMLElement.prototype.scrollIntoView = vi.fn();
  Object.defineProperty(document, "fonts", { configurable: true, value: new EventTarget() });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); Reflect.deleteProperty(document, "fonts"); });

it("常用模型先编辑草稿，撤销不保存，保存才提交偏好", async () => {
  const api = fakeApi();
  const saved = vi.fn();
  render(<MantineProvider env="test"><ModelSettingsDrawer opened onClose={() => {}} preferences={PREFERENCES}
    allModels={MODELS} onSaved={saved} api={api} /></MantineProvider>);
  const checkbox = await screen.findByRole("checkbox", { name: "在输入框显示 OpenAI Plain" });
  fireEvent.click(checkbox);
  expect(checkbox).toBeChecked();
  expect(api.savePreferences).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("tab", { name: /已选模型/ }));
  expect(screen.getByRole("button", { name: "测试 OpenAI Plain" })).toBeVisible();
  expect(screen.getByRole("button", { name: "测试 DeepSeek Beta" })).toBeVisible();
  expect(screen.getByRole("button", { name: "删除 OpenAI Alpha" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "删除 OpenAI Plain" }));
  expect(screen.queryByRole("button", { name: "测试 OpenAI Plain" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("tab", { name: "供应商与模型" }));
  expect(checkbox).not.toBeChecked();
  fireEvent.click(checkbox);
  fireEvent.click(screen.getByRole("button", { name: "撤销修改" }));
  expect(checkbox).not.toBeChecked();
  fireEvent.click(checkbox);
  fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
  await waitFor(() => expect(saved).toHaveBeenCalledWith(expect.objectContaining({ favorites: expect.objectContaining({ "openai/plain": { effort: null, compactTokens: 50_000 } }) })));
  expect(PREFERENCES.favorites["openai/plain"]).toBeUndefined();
  expect(screen.getByRole("checkbox", { name: "在输入框显示 OpenAI Alpha" })).toBeDisabled();
});

it("切换供应商后，迟到的旧目录不会覆盖新目录", async () => {
  const api = fakeApi();
  let resolveOpenAi!: (value: CatalogModel[]) => void;
  api.loadModels = vi.fn((provider) => provider === "openai"
    ? new Promise<CatalogModel[]>((resolve) => { resolveOpenAi = resolve; })
    : Promise.resolve(MODELS.filter((item) => item.provider === provider)));
  render(<MantineProvider env="test"><ModelSettingsDrawer opened onClose={() => {}} preferences={PREFERENCES}
    allModels={MODELS} onSaved={() => {}} api={api} /></MantineProvider>);
  fireEvent.click(screen.getByRole("combobox", { name: "供应商" }));
  fireEvent.click(await screen.findByRole("option", { name: "DeepSeek · API Key" }));
  expect(await screen.findByRole("checkbox", { name: "在输入框显示 DeepSeek Beta" })).toBeVisible();
  await act(async () => { resolveOpenAi(MODELS.filter((item) => item.provider === "openai")); });
  expect(screen.queryByRole("checkbox", { name: "在输入框显示 OpenAI Alpha" })).not.toBeInTheDocument();
  expect(screen.getByRole("checkbox", { name: "在输入框显示 DeepSeek Beta" })).toBeVisible();
});

it("模型菜单只展示勾选模型的名称，推理菜单保留供应商自己的档位", async () => {
  const select = vi.fn();
  const view = render(<MantineProvider env="test"><ModelPicker models={MODELS} preferences={PREFERENCES}
    selection={{ model: "deepseek/beta", effort: null }} onSelect={select} pending={false} /></MantineProvider>);
  fireEvent.click(screen.getByRole("button", { name: "推理强度：高" }));
  expect(await screen.findByRole("menuitem", { name: "最高", exact: true })).toBeVisible();
  expect(screen.queryByRole("menuitem", { name: "中", exact: true })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("menuitem", { name: "最高", exact: true }));
  expect(select).toHaveBeenCalledWith({ model: "deepseek/beta", effort: "max" });
  fireEvent.click(screen.getByRole("button", { name: "切换模型：DeepSeek Beta" }));
  const menu = await screen.findByRole("menu");
  expect(within(menu).getAllByRole("menuitem").map((item) => item.textContent)).toEqual(["Alpha", "Beta"]);
  expect(menu.textContent).toBe("AlphaBeta");
  expect(within(menu).queryByRole("menuitem", { name: "OpenAI Plain" })).not.toBeInTheDocument();
  expect(within(menu).getByRole("menuitem", { name: "DeepSeek Beta" })).toHaveAttribute("aria-current", "true");
  fireEvent.click(within(menu).getByRole("menuitem", { name: "OpenAI Alpha" }));
  expect(select).toHaveBeenLastCalledWith({ model: "openai/alpha", effort: null });
  view.rerender(<MantineProvider env="test"><ModelPicker models={MODELS} preferences={PREFERENCES}
    selection={{ model: "openai/plain", effort: null }} onSelect={select} pending={false} /></MantineProvider>);
  expect(screen.queryByRole("button", { name: /推理强度/ })).not.toBeInTheDocument();
});

it("修改新会话默认值保留当前选择，新建 Bot 才采用新默认值", async () => {
  render(<MantineProvider env="test"><ModelPreview api={fakeApi()} /></MantineProvider>);
  expect(await screen.findByRole("button", { name: "切换模型：OpenAI Alpha" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "打开模型配置" }));
  fireEvent.click(await screen.findByRole("tab", { name: /已选模型/ }));
  fireEvent.click(await screen.findByRole("combobox", { name: "新会话默认模型" }));
  fireEvent.click(await screen.findByRole("option", { name: "DeepSeek · Beta" }));
  fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
  await waitFor(() => expect(screen.getByText("常用模型已保存")).toBeVisible());
  fireEvent.click(screen.getByRole("button", { name: "关闭模型配置" }));
  expect(screen.getByRole("button", { name: "切换模型：OpenAI Alpha" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "新建体验 Bot" }));
  expect(await screen.findByRole("button", { name: "切换模型：DeepSeek Beta" })).toBeVisible();
  fireEvent.click(within(screen.getByRole("complementary", { name: "Bot 名册" })).getByRole("button", { name: "UI 体验" }));
  expect(screen.getByRole("button", { name: "切换模型：OpenAI Alpha" })).toBeVisible();
});

it("回复开始后改变强度仅影响下一次，已发送消息保留原选择", async () => {
  const api = fakeApi();
  let finishReply!: (value: string) => void;
  api.reply = vi.fn(() => new Promise<string>((resolve) => { finishReply = resolve; }));
  render(<MantineProvider env="test"><ModelPreview api={api} /></MantineProvider>);
  fireEvent.change(await screen.findByRole("textbox", { name: "消息" }), { target: { value: "测试强度选择" } });
  fireEvent.click(screen.getByRole("button", { name: "发送", exact: true }));
  fireEvent.click(screen.getByRole("button", { name: "推理强度：中" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: "高", exact: true }));
  expect(await screen.findByText("下次回复生效")).toBeVisible();
  expect(api.reply).toHaveBeenCalledWith("测试强度选择", MODELS[0], null, expect.any(AbortSignal));
  await act(async () => { finishReply("这条回复使用发送时的选择。 "); });
  expect(screen.getByRole("article", { name: "示例回复" })).toHaveTextContent("OpenAI · Alpha · 中 · 示例回复");
  expect(screen.getByRole("button", { name: "推理强度：高" })).toBeVisible();
  expect(screen.queryByText("下次回复生效")).not.toBeInTheDocument();
});

it("已选模型按当前强度独立测试，切换分页保留进度，失败可重试", async () => {
  const api = fakeApi();
  let finishAlpha!: (result: ModelTestResult) => void;
  let betaAttempts = 0;
  api.testModel = vi.fn((model) => {
    if (model.id === "openai/alpha") return new Promise<ModelTestResult>((resolve) => { finishAlpha = resolve; });
    return Promise.resolve<ModelTestResult>(++betaAttempts === 1
      ? { ok: false, message: "示例连接超时" } : { ok: true, latencyMs: 420 });
  });
  render(<MantineProvider env="test"><ModelSettingsDrawer opened onClose={() => {}} preferences={PREFERENCES}
    allModels={MODELS} onSaved={() => {}} api={api} /></MantineProvider>);
  fireEvent.click(screen.getByRole("tab", { name: /已选模型/ }));
  const alpha = () => within(screen.getByRole("listitem", { name: "OpenAI Alpha" }));
  const beta = () => within(screen.getByRole("listitem", { name: "DeepSeek Beta" }));
  fireEvent.click(alpha().getByRole("combobox", { name: "OpenAI Alpha 默认推理强度" }));
  fireEvent.click(await screen.findByRole("option", { name: "高", exact: true }));
  fireEvent.click(alpha().getByRole("button", { name: "测试 OpenAI Alpha" }));
  expect(api.testModel).toHaveBeenCalledWith(MODELS[0], "high");
  fireEvent.click(screen.getByRole("tab", { name: "供应商与模型" }));
  fireEvent.click(screen.getByRole("tab", { name: /已选模型/ }));
  expect(alpha().getByRole("status")).toHaveTextContent("正在测试");
  fireEvent.click(beta().getByRole("button", { name: "测试 DeepSeek Beta" }));
  expect(await beta().findByText("示例连接超时")).toBeVisible();
  expect(alpha().getByRole("status")).toHaveTextContent("正在测试");
  fireEvent.click(beta().getByRole("button", { name: "测试 DeepSeek Beta" }));
  await waitFor(() => expect(beta().getByRole("status")).toHaveTextContent("示例测试通过 · 420 ms"));
  await act(async () => { finishAlpha({ ok: true, latencyMs: 620 }); });
  expect(alpha().getByRole("status")).toHaveTextContent("示例测试通过 · 620 ms");
  expect(beta().getByRole("status")).toHaveTextContent("示例测试通过 · 420 ms");
});
