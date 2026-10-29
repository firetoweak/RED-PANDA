import type { ModelCatalog, ModelConfig, ModelProfile } from "../../api/contracts";

export type Effort = { value: string; label: string };
export type CatalogModel = { id: string; provider: string; name: string; description: string;
  efforts: Effort[]; defaultEffort: string | null; contextTokens: number | null; webSearch?: boolean };
export type Favorite = { effort: string | null; compactTokens: number };
export type Preferences = { favorites: Record<string, Favorite>; defaultModel: string | null };
export type ModelTestResult = {ok: true; latencyMs: number} | {ok: false; message: string};
export type ModelSettingsApi = {
  loadModels(provider: string): Promise<CatalogModel[]>;
  savePreferences(value: Preferences): Promise<Preferences>;
  testModel(model: CatalogModel, effort: string | null): Promise<ModelTestResult>;
};
export const PROVIDERS = [
  { value: "openai", label: "OpenAI", caption: "API Key" },
  { value: "openai-codex", label: "Codex", caption: "ChatGPT 登录" },
  { value: "deepseek", label: "DeepSeek", caption: "API Key" },
  { value: "stepfun", label: "StepFun", caption: "API Key" },
  { value: "qwen", label: "Qwen", caption: "百炼" },
  { value: "bigmodel", label: "智谱", caption: "API Key" },
  { value: "vllm", label: "vLLM", caption: "本地服务" },
  { value: "ollama", label: "Ollama", caption: "本地服务" },
];
const EFFORTS: Record<string, string> = {none:"关闭思考", minimal:"最少", low:"低", medium:"中", high:"高", xhigh:"极高", max:"最高", ultra:"超高"};
export function providerName(id: string) { return PROVIDERS.find((item) => item.value === id)!.label; }
export function effortName(model: CatalogModel, value: string | null) {
  return model.efforts.find((choice) => choice.value === (value ?? model.defaultEffort))?.label ?? "供应商默认";
}
export function catalogModels(catalog: ModelCatalog): CatalogModel[] {
  return catalog.map((item) => ({id:item.model, provider:item.model.split("/")[0], name:item.name,
    description: "", efforts:item.reasoning_levels.map((value) => ({value,label:EFFORTS[value] ?? value})),
    defaultEffort:item.default_reasoning_effort, contextTokens:item.context_window, webSearch:item.web_search}));
}
export function configuredModel(profile: ModelProfile): CatalogModel {
  return {id:profile.model, provider:profile.model.split("/")[0], name:profile.model.slice(profile.model.indexOf("/")+1),
    description:"", efforts:[], defaultEffort:null, contextTokens:null};
}
export function preferencesFrom(config: ModelConfig): Preferences {
  return {defaultModel: config.model.default, favorites: Object.fromEntries(config.model.candidates.map((item) => [item.model,
    {effort:item.reasoning_effort ?? null, compactTokens:item.compact_threshold_tokens}]))};
}
export function configFrom(preferences: Preferences): ModelConfig {
  return {model:{default:preferences.defaultModel, candidates:Object.entries(preferences.favorites).map(([model, item]) => ({
    model, compact_threshold_tokens:item.compactTokens, ...(item.effort === null ? {} : {reasoning_effort:item.effort}),
  }))}};
}
