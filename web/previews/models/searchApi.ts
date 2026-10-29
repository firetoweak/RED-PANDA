/** Preview-only search settings and results. No credentials or network requests.
 * OpenAI integration will use Responses web_search with tool_choice="auto";
 * only the search model is configurable.
 */
export type SearchProvider = "deepseek" | "openai";
export type SearchSettings = { enabled: boolean; provider: SearchProvider; openaiModel: string };
export type SearchTestResult = {
  query: string;
  provider: SearchProvider;
  model: string | null;
  latencyMs: number;
  summary: string;
  sources: { title: string; url: string; snippet: string }[];
};
export type SearchPreviewApi = {
  loadSettings(): Promise<SearchSettings>;
  saveSettings(settings: SearchSettings): Promise<SearchSettings>;
  testSearch(query: string, settings: SearchSettings): Promise<SearchTestResult>;
};

const INITIAL_SETTINGS: SearchSettings = { enabled: true, provider: "deepseek", openaiModel: "openai/gpt-6.1-terra" };
const STORAGE_KEY = "konden.model-ui-preview.search";
const delay = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));
let searchErrorShown = false;

export const searchPreviewApi: SearchPreviewApi = {
  async loadSettings() {
    const value = window.localStorage.getItem(STORAGE_KEY);
    return value === null ? structuredClone(INITIAL_SETTINGS) : JSON.parse(value) as SearchSettings;
  },
  async saveSettings(settings) {
    await delay(380);
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
    return structuredClone(settings);
  },
  async testSearch(query, settings) {
    await delay(1100);
    if (new URLSearchParams(window.location.search).get("scenario") === "search-error" && !searchErrorShown) {
      searchErrorShown = true;
      throw new Error("模拟搜索服务超时，请重试。");
    }
    return {
      query, provider: settings.provider, model: settings.provider === "openai" ? settings.openaiModel : null,
      latencyMs: settings.provider === "openai" ? 640 : 280,
      summary: `这是关于“${query}”的示例搜索摘要。接入真实服务后，这里会展示检索到的信息及对应来源。`,
      sources: [{
        title: settings.provider === "openai" ? "OpenAI Web Search 文档" : "DeepSeek 开发文档",
        url: settings.provider === "openai" ? "https://developers.openai.com/api/docs/guides/tools-web-search" : "https://api-docs.deepseek.com/",
        snippet: "示例来源，用于预览标题、链接和摘要的展示方式，未经实时检索。",
      }],
    };
  },
};
