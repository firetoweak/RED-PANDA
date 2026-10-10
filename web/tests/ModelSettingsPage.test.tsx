import { MantineProvider } from "@mantine/core";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ModelSettingsPage } from "../src/features/models/ModelSettingsPage";

const { data, save } = vi.hoisted(() => ({
  data: {
    config: { model: { default: "stepfun/step-5-preview", candidates: [
      { model: "stepfun/step-5-preview", compact_threshold_tokens: 200000 },
    ] } },
    providers: [{ provider: "stepfun", configured: true, missing: null, local: false }],
    connections_path: "/personal/connections.json",
  },
  save: vi.fn(),
}));

vi.mock("../src/api/redpandaApi", () => ({
  useGetModelSettingsQuery: () => ({ data, isLoading: false }),
  useSaveModelSettingsMutation: () => [save, { isLoading: false }],
}));
vi.mock("../src/app/hooks", () => ({ useAppSelector: () => "connection" }));
vi.mock("../src/features/models/ModelTestButton", () => ({ ModelTestButton: () => null }));

beforeEach(() => {
  data.config = { model: { default: "stepfun/step-5-preview", candidates: [
    { model: "stepfun/step-5-preview", compact_threshold_tokens: 200000 },
  ] } };
  save.mockReset().mockImplementation(({ config }) => ({
    unwrap: async () => { data.config = config; return { ...data, config }; },
  }));
  vi.stubGlobal("matchMedia", vi.fn(() => ({
    matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn(),
  })));
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("saves the selected Step 5 effort and omits it when reset to provider default", async () => {
  render(<MantineProvider env="test"><ModelSettingsPage /></MantineProvider>);
  fireEvent.click(screen.getByRole("button", { name: "编辑" }));
  fireEvent.click(screen.getByRole("combobox", { name: "推理程度" }));
  fireEvent.click(screen.getByRole("option", { name: "高", exact: true }));
  fireEvent.click(screen.getByRole("button", { name: "加入待保存配置" }));
  fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
  await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
  expect(save.mock.calls[0][0].config.model.candidates[0]).toEqual({
    model: "stepfun/step-5-preview", compact_threshold_tokens: 200000, reasoning_effort: "high",
  });

  await screen.findByText("已保存。默认模型用于新会话；模型参数在下一次决策生效。");
  fireEvent.click(screen.getByRole("button", { name: "编辑" }));
  expect(screen.getByRole("combobox", { name: "推理程度" })).toHaveValue("高");
  fireEvent.click(screen.getByRole("combobox", { name: "推理程度" }));
  fireEvent.click(screen.getByRole("option", { name: "供应商默认", exact: true }));
  fireEvent.click(screen.getByRole("button", { name: "加入待保存配置" }));
  fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
  await waitFor(() => expect(save).toHaveBeenCalledTimes(2));
  expect(save.mock.calls[1][0].config.model.candidates[0]).toEqual({
    model: "stepfun/step-5-preview", compact_threshold_tokens: 200000,
  });
});
