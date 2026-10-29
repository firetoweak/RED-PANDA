import { MantineProvider } from "@mantine/core";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ModelSelector } from "../src/features/models/ModelSelector";

const { select } = vi.hoisted(() => ({ select: vi.fn() }));
vi.mock("../src/api/kondenApi", () => ({
  useGetModelCatalogQuery: () => ({currentData:[
    {model:"deepseek/pro",name:"pro",context_window:100000,reasoning_levels:["low","max"],default_reasoning_effort:"low"},
    {model:"deepseek/flash",name:"flash",context_window:100000,reasoning_levels:[],default_reasoning_effort:null},
  ]}),
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

it("模型设置折叠为单个入口，经模型子菜单切换仍提交完整模型标识", async () => {
  render(<MantineProvider env="test"><ModelSelector sessionId="session" connectionId="connection" /></MantineProvider>);
  const button = screen.getByRole("button", { name: "模型设置：DeepSeek pro，推理强度：低" });
  expect(button).toHaveTextContent("pro");
  expect(button).not.toHaveTextContent("deepseek/");
  expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  fireEvent.mouseEnter(button);
  expect(screen.queryByRole("menuitem", { name: "DeepSeek flash" })).not.toBeInTheDocument();
  fireEvent.click(button);
  expect(screen.queryByRole("menuitem", { name: "DeepSeek flash" })).not.toBeInTheDocument();
  fireEvent.click(await screen.findByRole("menuitem", { name: "切换模型：DeepSeek pro" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: "DeepSeek flash" }));
  await waitFor(() => expect(select).toHaveBeenCalledWith({
    sessionId: "session", connectionId: "connection", model: "deepseek/flash", reasoningEffort:null,
  }));
});


it("会话强度菜单使用供应商自己的档位并随选择提交", async () => {
  render(<MantineProvider env="test"><ModelSelector sessionId="session" connectionId="connection" /></MantineProvider>);
  fireEvent.click(screen.getByRole("button",{name:"模型设置：DeepSeek pro，推理强度：低"}));
  fireEvent.click(await screen.findByRole("menuitem",{name:"推理强度：低"}));
  expect(screen.queryByRole("menuitem",{name:"中",exact:true})).not.toBeInTheDocument();
  fireEvent.click(await screen.findByRole("menuitem",{name:"最高",exact:true}));
  await waitFor(() => expect(select).toHaveBeenCalledWith({sessionId:"session",connectionId:"connection",model:"deepseek/pro",reasoningEffort:"max"}));
});
