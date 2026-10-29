/** Isolated preview fixtures. No production API, credentials, or model calls. */
export type Effort = { value: string; label: string };
export type CatalogModel = {
  id: string;
  provider: string;
  name: string;
  description: string;
  efforts: Effort[];
  defaultEffort: string | null;
  contextTokens: number;
};
export type Favorite = { effort: string | null; compactTokens: number };
export type Preferences = { favorites: Record<string, Favorite>; defaultModel: string };
export type ModelTestResult = { ok: true; latencyMs: number } | { ok: false; message: string };
export type ModelPreviewApi = {
  loadModels(provider?: string): Promise<CatalogModel[]>;
  loadPreferences(): Promise<Preferences>;
  savePreferences(value: Preferences): Promise<Preferences>;
  testModel(model: CatalogModel, effort: string | null): Promise<ModelTestResult>;
  reply(text: string, model: CatalogModel, effort: string | null, signal: AbortSignal): Promise<string>;
};

export const PROVIDERS = [
  { value: "openai", label: "OpenAI", caption: "API Key" },
  { value: "openai-codex", label: "Codex", caption: "ChatGPT 登录" },
  { value: "deepseek", label: "DeepSeek", caption: "API Key" },
  { value: "stepfun", label: "StepFun", caption: "API Key" },
  { value: "qwen", label: "Qwen", caption: "百炼" },
  { value: "ollama", label: "Ollama", caption: "本地服务" },
];
const CHOICES: Record<string, string> = { none: "关闭思考", low: "低", medium: "中", high: "高", xhigh: "极高", max: "最高" };
const efforts = (...values: string[]) => values.map((value) => ({ value, label: CHOICES[value] }));
const model = (provider: string, slug: string, name: string, description: string,
  levels: Effort[], defaultEffort: string | null, contextTokens = 1_050_000): CatalogModel => ({
  id: `${provider}/${slug}`, provider, name, description, efforts: levels, defaultEffort, contextTokens,
});

const MODELS = [
  model("openai", "gpt-6.1-terra", "GPT-6.1 Terra", "日常讨论与视觉任务", efforts("none", "low", "medium", "high", "xhigh", "max"), "medium"),
  model("openai", "gpt-6.1-sol", "GPT-6.1 Sol", "复杂任务与代码协作", efforts("low", "medium", "high", "xhigh", "max"), "medium"),
  model("openai", "gpt-6-astra", "GPT-6 Astra", "深入分析与长任务", efforts("low", "medium", "high", "xhigh", "max"), "medium"),
  model("openai", "gpt-6-luna", "GPT-6 Luna", "快速完成明确的任务", efforts("none", "low", "medium", "high", "xhigh", "max"), "medium"),
  model("openai", "gpt-5.6-terra", "GPT-5.6 Terra", "通用任务", efforts("none", "low", "medium", "high", "xhigh", "max"), "medium"),
  model("openai", "gpt-4.1", "GPT-4.1", "未提供推理档位", [], null, 1_047_576),
  model("openai-codex", "gpt-6.1-sol", "GPT-6.1 Sol", "使用 ChatGPT 登录", efforts("low", "medium", "high", "xhigh", "max"), "medium"),
  model("openai-codex", "gpt-6.1-terra", "GPT-6.1 Terra", "使用 ChatGPT 登录", efforts("none", "low", "medium", "high", "xhigh", "max"), "medium"),
  model("openai-codex", "gpt-6-luna", "GPT-6 Luna", "使用 ChatGPT 登录", efforts("none", "low", "medium", "high", "xhigh", "max"), "medium"),
  model("deepseek", "deepseek-flash", "DeepSeek Flash", "日常协作与工具调用", efforts("low", "high", "max"), "high", 1_000_000),
  model("deepseek", "deepseek-v4-pro", "DeepSeek V4 Pro", "深入分析与代码任务", efforts("low", "high", "max"), "high", 1_000_000),
  model("stepfun", "step-5-preview", "Step 5 Preview", "三个推理档位", efforts("low", "medium", "high"), "medium", 256_000),
  model("qwen", "qwen3.8-max", "Qwen3.8 Max", "使用百炼连接", efforts("none", "low", "medium", "xhigh"), "xhigh", 262_144),
  model("ollama", "qwen3:8b", "Qwen3 · 8B", "服务未提供推理档位", [], null, 32_768),
];

export const INITIAL_PREFERENCES: Preferences = {
  favorites: {
    "openai/gpt-6.1-terra": { effort: null, compactTokens: 500_000 },
    "openai-codex/gpt-6.1-sol": { effort: "high", compactTokens: 500_000 },
    "deepseek/deepseek-flash": { effort: null, compactTokens: 500_000 },
  },
  defaultModel: "openai/gpt-6.1-terra",
};

export function providerName(id: string) { return PROVIDERS.find((provider) => provider.value === id)!.label; }
export function effortName(item: CatalogModel, value: string | null) {
  return item.efforts.find((choice) => choice.value === (value ?? item.defaultEffort))?.label ?? "供应商默认";
}

const STORAGE_KEY = "konden.model-ui-preview.preferences";
let catalogErrorShown = false;
const failedModelTests = new Set<string>();
const delay = (milliseconds: number) => new Promise<void>((resolve) => window.setTimeout(resolve, milliseconds));

export const previewApi: ModelPreviewApi = {
  async loadModels(provider) {
    await delay(provider === undefined ? 180 : 480);
    if (provider && new URLSearchParams(window.location.search).get("scenario") === "catalog-error" && !catalogErrorShown) {
      catalogErrorShown = true;
      throw new Error("暂时无法读取模型目录，请重新加载。");
    }
    return structuredClone(MODELS.filter((item) => provider === undefined || item.provider === provider));
  },
  async loadPreferences() {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    return saved === null ? structuredClone(INITIAL_PREFERENCES) : JSON.parse(saved) as Preferences;
  },
  async savePreferences(value) {
    await delay(380);
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
    return structuredClone(value);
  },
  async testModel(item, _effort) {
    await delay(1000);
    if (item.provider === "ollama" && !failedModelTests.has(item.id)) {
      failedModelTests.add(item.id);
      return { ok: false, message: "模拟本地服务未响应，请重试。" };
    }
    return { ok: true, latencyMs: 420 };
  },
  async reply(_text, selectedModel, effort, signal) {
    await new Promise<void>((resolve, reject) => {
      const timer = window.setTimeout(resolve, 1800);
      signal.addEventListener("abort", () => {
        window.clearTimeout(timer);
        reject(new DOMException("预览回复已停止", "AbortError"));
      }, { once: true });
    });
    return `这是一条示例回复。当前使用 ${providerName(selectedModel.provider)} 的 ${selectedModel.name}${selectedModel.efforts.length ? `，推理强度为「${effortName(selectedModel, effort)}」` : ""}。你可以继续切换模型，或在模型配置里调整常用列表。`;
  },
};
