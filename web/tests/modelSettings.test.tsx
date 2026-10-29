import { MantineProvider } from "@mantine/core";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ModelSettingsPanel as ModelSettingsDrawer } from "../src/features/models/ModelSettingsPanel";
import { ModelPicker } from "../src/features/models/ModelPicker";
import { SearchSettingsPanel } from "../src/features/models/SearchSettingsPanel";
import type { CatalogModel, ModelSettingsApi, ModelTestResult, Preferences } from "../src/features/models/modelUi";
import type { SearchApi, SearchSettings, SearchTestResult } from "../src/features/models/searchTypes";

const CONNECTIONS = [{provider:"openai",configured:true},{provider:"deepseek",configured:true}];
const MODELS: CatalogModel[] = [
  { id: "openai/alpha", provider: "openai", webSearch:true, name: "Alpha", description: "alpha", contextTokens: 100_000,
    efforts: [{ value: "medium", label: "中" }, { value: "high", label: "高" }], defaultEffort: "medium" },
  { id: "openai/plain", provider: "openai", webSearch:true, name: "Plain", description: "plain", contextTokens: 100_000,
    efforts: [], defaultEffort: null },
  { id: "deepseek/beta", provider: "deepseek", name: "Beta", description: "beta", contextTokens: 100_000,
    efforts: [{ value: "low", label: "低" }, { value: "high", label: "高" }, { value: "max", label: "最高" }], defaultEffort: "high" },
];
const PREFERENCES: Preferences = {
  favorites: { "openai/alpha": { effort: null, compactTokens: 70_000 }, "deepseek/beta": { effort: null, compactTokens: 70_000 } },
  defaultModel: "openai/alpha",
};
function fakeApi(): ModelSettingsApi {
  return {
    loadModels: vi.fn(async (provider?: string) => MODELS.filter((item) => !provider || item.provider === provider)),
    savePreferences: vi.fn(async (value) => structuredClone(value)),
    testModel: vi.fn(async () => ({ ok: true as const, latencyMs: 420 })),
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
  render(<MantineProvider env="test"><ModelSettingsDrawer active connected searchApi={fakeSearchApi()} connections={CONNECTIONS} preferences={PREFERENCES}
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
  render(<MantineProvider env="test"><ModelSettingsDrawer active connected searchApi={fakeSearchApi()} connections={CONNECTIONS} preferences={PREFERENCES}
    allModels={MODELS} onSaved={() => {}} api={api} /></MantineProvider>);
  fireEvent.click(screen.getByRole("combobox", { name: "供应商" }));
  fireEvent.click(await screen.findByRole("option", { name: "DeepSeek · API Key" }));
  expect(await screen.findByRole("checkbox", { name: "在输入框显示 DeepSeek Beta" })).toBeVisible();
  await act(async () => { resolveOpenAi(MODELS.filter((item) => item.provider === "openai")); });
  expect(screen.queryByRole("checkbox", { name: "在输入框显示 OpenAI Alpha" })).not.toBeInTheDocument();
  expect(screen.getByRole("checkbox", { name: "在输入框显示 DeepSeek Beta" })).toBeVisible();
});

it("折叠菜单按需展示生效提示与勾选模型，推理子菜单保留供应商自己的档位", async () => {
  const select = vi.fn();
  const view = render(<MantineProvider env="test"><ModelPicker models={MODELS} preferences={PREFERENCES}
    selection={{ model: "deepseek/beta", effort: null }} onSelect={select} pending /></MantineProvider>);
  expect(screen.getAllByRole("button")).toHaveLength(1);
  expect(screen.queryByText("下次回复生效")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "模型设置：DeepSeek Beta，推理强度：高" }));
  expect(await screen.findByText("下次回复生效")).toBeVisible();
  expect(screen.queryByText(/Context|Fast|上下文/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("menuitem", { name: "推理强度：高" }));
  expect(await screen.findByRole("menuitem", { name: "最高", exact: true })).toBeVisible();
  expect(screen.queryByRole("menuitem", { name: "中", exact: true })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("menuitem", { name: "最高", exact: true }));
  expect(select).toHaveBeenCalledWith({ model: "deepseek/beta", effort: "max" });
  fireEvent.click(screen.getByRole("button", { name: "模型设置：DeepSeek Beta，推理强度：高" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: "切换模型：DeepSeek Beta" }));
  const menu = (await screen.findByRole("menuitem", { name: "OpenAI Alpha" })).closest('[role="menu"]') as HTMLElement;
  expect(within(menu).getAllByRole("menuitem").map((item) => item.textContent)).toEqual(["Alpha", "Beta"]);
  expect(menu.textContent).toBe("AlphaBeta");
  expect(within(menu).queryByRole("menuitem", { name: "OpenAI Plain" })).not.toBeInTheDocument();
  expect(within(menu).getByRole("menuitem", { name: "DeepSeek Beta" })).toHaveAttribute("aria-current", "true");
  fireEvent.click(within(menu).getByRole("menuitem", { name: "OpenAI Alpha" }));
  expect(select).toHaveBeenLastCalledWith({ model: "openai/alpha", effort: null });
  view.rerender(<MantineProvider env="test"><ModelPicker models={MODELS} preferences={PREFERENCES}
    selection={{ model: "openai/plain", effort: null }} onSelect={select} pending={false} /></MantineProvider>);
  fireEvent.click(screen.getByRole("button", { name: "模型设置：OpenAI Plain" }));
  expect(screen.queryByRole("menuitem", { name: /推理强度/ })).not.toBeInTheDocument();
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
  render(<MantineProvider env="test"><ModelSettingsDrawer active connected searchApi={fakeSearchApi()} connections={CONNECTIONS} preferences={PREFERENCES}
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
  await waitFor(() => expect(beta().getByRole("status")).toHaveTextContent("测试通过 · 420 ms"));
  await act(async () => { finishAlpha({ ok: true, latencyMs: 620 }); });
  expect(alpha().getByRole("status")).toHaveTextContent("测试通过 · 620 ms");
  expect(beta().getByRole("status")).toHaveTextContent("测试通过 · 420 ms");
});

const SEARCH_SETTINGS: SearchSettings = { enabled: true, provider: "deepseek", openaiModel: "openai/alpha" };
const SEARCH_RESULT: SearchTestResult = {
  query: "测试搜索词", searched:true, provider: "deepseek", model: null, latencyMs: 280, summary: "示例搜索结果",
  sources: [{ title: "测试来源", url: "https://example.com/reference", snippet: "来源摘要" }],
};
function fakeSearchApi(): SearchApi {
  return {
    loadSettings: vi.fn(async () => structuredClone(SEARCH_SETTINGS)),
    saveSettings: vi.fn(async (value) => structuredClone(value)),
    testSearch: vi.fn(async () => structuredClone(SEARCH_RESULT)),
  };
}

it("搜索配置独立保存，撤销可还原，停用仍保留服务和非常用搜索模型", async () => {
  const api = fakeSearchApi();
  render(<MantineProvider env="test"><SearchSettingsPanel models={MODELS} api={api} connected connections={CONNECTIONS} loadModels={fakeApi().loadModels} /></MantineProvider>);
  fireEvent.click(await screen.findByRole("combobox", { name: "搜索服务" }));
  fireEvent.click(await screen.findByRole("option", { name: "OpenAI", exact: true }));
  await waitFor(() => expect(screen.getByRole("combobox", { name: "搜索模型" })).toBeEnabled());
  fireEvent.click(screen.getByRole("combobox", { name: "搜索模型" }));
  fireEvent.click(await screen.findByRole("option", { name: "Plain", exact: true }));
  expect(api.saveSettings).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "撤销修改" }));
  expect(screen.getByRole("combobox", { name: "搜索服务" })).toHaveValue("DeepSeek");
  expect(screen.queryByRole("combobox", { name: "搜索模型" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("combobox", { name: "搜索服务" }));
  fireEvent.click(await screen.findByRole("option", { name: "OpenAI", exact: true }));
  await waitFor(() => expect(screen.getByRole("combobox", { name: "搜索模型" })).toBeEnabled());
  fireEvent.click(screen.getByRole("combobox", { name: "搜索模型" }));
  fireEvent.click(await screen.findByRole("option", { name: "Plain", exact: true }));
  fireEvent.click(screen.getByRole("switch", { name: "启用网络搜索" }));
  expect(screen.getByRole("button", { name: "测试搜索" })).toBeDisabled();
  expect(screen.getByRole("combobox", { name: "搜索模型" })).toHaveValue("Plain");
  fireEvent.click(screen.getByRole("button", { name: "保存搜索配置" }));
  await waitFor(() => expect(api.saveSettings).toHaveBeenCalledWith({ enabled: false, provider: "openai", openaiModel: "openai/plain" }));
  expect(await screen.findByText("搜索配置已保存")).toBeVisible();
  expect(api.testSearch).not.toHaveBeenCalled();
});

it("搜索测试可失败重试，返回结果带来源，换服务后清除旧结果", async () => {
  const api = fakeSearchApi();
  api.testSearch = vi.fn<SearchApi["testSearch"]>().mockRejectedValueOnce(new Error("示例服务超时"))
    .mockResolvedValueOnce(SEARCH_RESULT);
  render(<MantineProvider env="test"><SearchSettingsPanel models={MODELS} api={api} connected connections={CONNECTIONS} loadModels={fakeApi().loadModels} /></MantineProvider>);
  fireEvent.change(await screen.findByRole("textbox", { name: "测试问题" }), { target: { value: " 测试搜索词 " } });
  fireEvent.click(screen.getByRole("button", { name: "测试搜索" }));
  expect(await screen.findByText("示例服务超时")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "重试搜索" }));
  const result = await screen.findByRole("region", { name: "搜索测试结果" });
  expect(within(result).getByText("示例搜索结果")).toBeVisible();
  expect(within(result).getByRole("link", { name: "测试来源" })).toHaveAttribute("href", "https://example.com/reference");
  expect(api.testSearch).toHaveBeenNthCalledWith(2, "测试搜索词", SEARCH_SETTINGS);
  fireEvent.click(screen.getByRole("combobox", { name: "搜索服务" }));
  fireEvent.click(await screen.findByRole("option", { name: "OpenAI", exact: true }));
  expect(screen.queryByRole("region", { name: "搜索测试结果" })).not.toBeInTheDocument();
});

it("自动搜索直接回答时明确显示未联网，不当成检索失败", async () => {
  const api = fakeSearchApi();
  api.loadSettings = vi.fn(async () => ({...SEARCH_SETTINGS,provider:"openai" as const}));
  api.testSearch = vi.fn(async () => ({...SEARCH_RESULT,provider:"openai" as const,model:"openai/alpha",searched:false,summary:"直接回答",sources:[]}));
  render(<MantineProvider env="test"><SearchSettingsPanel models={MODELS} api={api} connected connections={CONNECTIONS} loadModels={fakeApi().loadModels}/></MantineProvider>);
  fireEvent.click(await screen.findByRole("button",{name:"测试搜索",exact:true}));
  const result = await screen.findByRole("region",{name:"搜索测试结果"});
  expect(result).toHaveTextContent("模型直接回答，未联网");
  expect(within(result).queryByRole("link")).not.toBeInTheDocument();
  expect(screen.queryByText("搜索未完成")).not.toBeInTheDocument();
});
