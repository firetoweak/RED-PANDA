export type SearchProvider = "deepseek" | "openai";
export type SearchSettings = {enabled: boolean; provider: SearchProvider; openaiModel: string | null};
export type SearchTestResult = {query:string; provider:SearchProvider; model:string|null; latencyMs:number;
  searched:boolean; summary:string|null; sources:{title:string;url:string;snippet:string}[]};
export type SearchApi = {
  loadSettings(): Promise<SearchSettings>;
  saveSettings(value: SearchSettings): Promise<SearchSettings>;
  testSearch(query:string, settings:SearchSettings):Promise<SearchTestResult>;
};
