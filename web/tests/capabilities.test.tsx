import { MantineProvider } from "@mantine/core";
import { configureStore } from "@reduxjs/toolkit";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { Provider } from "react-redux";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { CapabilityInventory } from "../src/api/capabilities";
import { CapabilitiesPage } from "../src/features/capabilities/CapabilitiesPage";
import runtimeReducer, { connected } from "../src/realtime/runtimeSlice";

const state = vi.hoisted(() => ({
  data: undefined as CapabilityInventory | undefined,
  isError: false, isLoading: false,
  setEnabled: vi.fn(), refetch: vi.fn(), mutationError: undefined as unknown,
}));
vi.mock("../src/api/kondenApi", () => ({
  useGetCapabilitiesQuery: () => ({ data: state.data, isLoading: state.isLoading, isError: state.isError, isFetching: false, refetch: state.refetch }),
  useSetCapabilityEnabledMutation: () => [state.setEnabled, { isLoading: false, error: state.mutationError }],
}));

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  state.isError = false; state.isLoading = false; state.mutationError = undefined;
  state.setEnabled.mockClear(); state.refetch.mockClear();
  state.data = {
    skills: [
      { id: "builtin:coding", name: "coding", description: "编码方法", origin: "builtin", enabled: true, source: "随 konden 提供", version: null, path: "/bundled/coding" },
      { id: "review", name: "review", description: "独立验证", origin: "installed", enabled: false, source: "/source", version: "v1", path: "/packages/review" },
    ],
    mcp: [{ id: "reports", name: "Reports", description: "报告查询", origin: "installed", enabled: true,
      transport: "streamable_http", read_only: true, last_status: "unknown", last_checked_at: null, last_error_summary: null }],
    cli: [{ id: "git", name: "git", description: "版本控制", source: "/git.json", version: "2.50.0", path: "/bin/git", health: null }],
  };
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function app() {
  const store = configureStore({ reducer: { runtime: runtimeReducer } });
  store.dispatch(connected("web-connection"));
  return <Provider store={store}><MantineProvider env="test"><CapabilitiesPage /></MantineProvider></Provider>;
}

it("清单含停用项和内置项，搜索用途并展开来源，开关使用准确身份", () => {
  render(app());
  expect(screen.getByRole("button", { name: "查看 coding" })).toBeInTheDocument();
  expect(screen.getAllByRole("switch")).toHaveLength(1);
  const toggle = screen.getByRole("switch", { name: "启用 review" });
  expect(toggle).not.toBeChecked();
  fireEvent.click(toggle);
  expect(state.setEnabled).toHaveBeenCalledWith({ connectionId: "web-connection", kind: "skills", id: "review", enabled: true });
  fireEvent.change(screen.getByRole("textbox", { name: "搜索 Skill" }), { target: { value: "独立" } });
  expect(screen.queryByRole("button", { name: "查看 coding" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "查看 review" }));
  expect(screen.getByText("/source")).toBeVisible();
  expect(screen.getByText("/packages/review")).toBeVisible();
});

it("MCP开启仍显示未检测，CLI显示版本并且没有启停开关", () => {
  render(app());
  fireEvent.click(screen.getByRole("tab", { name: /MCP/ }));
  const list = within(screen.getByRole("list", { name: "MCP 清单" }));
  expect(list.getByText("未检测")).toBeVisible();
  expect(list.getByRole("switch", { name: "停用 Reports" })).toBeChecked();
  fireEvent.click(screen.getByRole("tab", { name: /CLI/ }));
  expect(screen.getByText("2.50.0")).toBeVisible();
  expect(screen.queryByRole("switch")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "查看 git" }));
  expect(screen.getByText("/bin/git")).toBeVisible();
});

it("区分读取失败、空清单与无搜索结果，并保留开关失败的实际状态", () => {
  state.mutationError = "浏览器连接已失效";
  const view = render(app());
  expect(screen.getByRole("alert")).toHaveTextContent("浏览器连接已失效");
  expect(screen.getByRole("switch", { name: "启用 review" })).not.toBeChecked();
  state.isError = true; state.data = undefined;
  view.rerender(app());
  expect(screen.getByRole("alert")).toHaveTextContent("能力清单加载失败");
  expect(screen.queryByText(/还没有/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  expect(state.refetch).toHaveBeenCalled();
  state.isError = false; state.data = { skills: [], mcp: [], cli: [] };
  view.rerender(app());
  expect(screen.getByRole("status")).toHaveTextContent("还没有已安装的 Skill");
  fireEvent.change(screen.getByRole("textbox", { name: "搜索 Skill" }), { target: { value: "missing" } });
  expect(screen.getByRole("status")).toHaveTextContent("没有找到匹配的能力");
});
