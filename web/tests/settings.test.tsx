import { MantineProvider } from "@mantine/core";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { SettingsMenu } from "../src/features/settings/SettingsMenu";
import { sharedMemorySchema } from "../src/api/contracts";

const state = vi.hoisted(() => ({
  data: { version: 1, content: "共享资源先协调。", updated_by: "Ada" },
  error: undefined as unknown,
  save: vi.fn(), reset: vi.fn(), refetch: vi.fn(),
}));
vi.mock("../src/features/models/ModelSettings", () => ({ ModelSettings: () => <div>模型配置内容</div> }));
vi.mock("../src/app/hooks", () => ({ useAppSelector: () => "connection" }));
vi.mock("../src/api/kondenApi", () => ({
  useGetSharedMemoryQuery: () => ({ data: state.data, error: state.error, refetch: state.refetch }),
  useSaveSharedMemoryMutation: () => [state.save, { isLoading: false, error: undefined, reset: state.reset }],
}));
beforeEach(() => {
  state.data = { version: 1, content: "共享资源先协调。", updated_by: "Ada" };
  state.error = undefined;
  state.save.mockReset().mockImplementation(({ version, content }) => ({
    unwrap: async () => ({ version: version + 1, content, updated_by: "用户" }),
  }));
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  Object.defineProperty(document, "fonts", { configurable: true, value: new EventTarget() });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); Reflect.deleteProperty(document, "fonts"); });

function app() {
  return <MantineProvider env="test"><SettingsMenu onOpen={() => {}} /></MantineProvider>;
}
async function open(name: string) {
  fireEvent.click(screen.getByRole("button", { name: "设置", exact: true }));
  fireEvent.click(await screen.findByRole("menuitem", { name, exact: true }));
}

it("统一设置入口切换抽屉并保留共同记忆草稿", async () => {
  render(app());
  await open("共同记忆");
  const input = await screen.findByRole("textbox", { name: "共同记忆正文" });
  fireEvent.change(input, { target: { value: "尚未保存的共同约定。" } });
  fireEvent.click(screen.getByRole("button", { name: "关闭设置" }));
  await open("模型配置");
  expect(await screen.findByText("模型配置内容")).toBeVisible();
  expect(screen.queryByRole("textbox", { name: "共同记忆正文" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "关闭设置" }));
  await open("共同记忆");
  expect(await screen.findByRole("textbox", { name: "共同记忆正文" })).toHaveValue("尚未保存的共同约定。");
  expect(state.save).not.toHaveBeenCalled();
});

it("500字按Unicode字符计数，超过上限不能保存并可清空", async () => {
  render(app());
  await open("共同记忆");
  const input = await screen.findByRole("textbox", { name: "共同记忆正文" });
  fireEvent.change(input, { target: { value: "🐼".repeat(500) } });
  expect(sharedMemorySchema.parse({ ...state.data, content: "🐼".repeat(500) }).content).toHaveLength(1000);
  expect(screen.getByText("500 / 500")).toBeVisible();
  expect(screen.getByRole("button", { name: "保存记忆" })).toBeEnabled();
  fireEvent.change(input, { target: { value: "字".repeat(501) } });
  expect(screen.getByText("超过500字，请精简后保存。")).toBeVisible();
  expect(screen.getByRole("button", { name: "保存记忆" })).toBeDisabled();
  fireEvent.change(input, { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "保存记忆" }));
  await waitFor(() => expect(state.save).toHaveBeenCalledWith(expect.objectContaining({ version: 1, content: "" })));
  expect(await screen.findByRole("status")).toHaveTextContent("已保存");
});

it("Bot更新后保留草稿，展示最新内容，核对后才按新版本保存", async () => {
  const view = render(app());
  await open("共同记忆");
  fireEvent.change(await screen.findByRole("textbox", { name: "共同记忆正文" }), { target: { value: "用户的整理稿。" } });
  state.data = { version: 2, content: "Bot刚更新的团队约定。", updated_by: "Lin" };
  view.rerender(app());
  expect(await screen.findByText("Bot刚更新的团队约定。")).toBeVisible();
  expect(screen.getByRole("textbox", { name: "共同记忆正文" })).toHaveValue("用户的整理稿。");
  expect(screen.getByRole("button", { name: "保存记忆" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "已核对，继续编辑" }));
  fireEvent.click(screen.getByRole("button", { name: "保存记忆" }));
  await waitFor(() => expect(state.save).toHaveBeenCalledWith(expect.objectContaining({ version: 2, content: "用户的整理稿。" })));
});
