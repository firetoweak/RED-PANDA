import { MantineProvider } from "@mantine/core";
import { configureStore } from "@reduxjs/toolkit";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { Provider } from "react-redux";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { SessionConversation } from "../src/features/conversation/SessionConversation";
import runtimeReducer, { connected } from "../src/realtime/runtimeSlice";

const commands = vi.hoisted(() => ({ stop: vi.fn(() => Promise.resolve({ data: {} })), refetch: vi.fn() }));

vi.mock("../src/features/conversation/Conversation", () => ({ Conversation: () => <p>对话内容</p> }));
vi.mock("../src/api/kondenApi", () => ({
  useGetConversationQuery: () => ({ currentData: undefined }),
  useGetAgentsQuery: () => ({ data: [
    { session_id: "ada", projects: [{ name: "Alpha", workspace_id: "alpha", directory: "C:/bots/ada/projects/Alpha" }],
      name: "Ada", status: "waiting" },
    { session_id: "lin", projects: [{ name: "Beta", workspace_id: "beta", directory: "C:/bots/lin/projects/Beta" }],
      name: "Lin", status: "idle" },
  ] }),
  useGetWorkspacesQuery: () => ({ data: [
    { workspace_id: "alpha", name: "Alpha", task_root: "C:/alpha" },
    { workspace_id: "beta", name: "Beta", task_root: "C:/beta" },
  ] }),
  useGetGroupsQuery: () => ({ data: [
    { group_id: "front", name: "界面讨论", members: ["Ada"] },
    { group_id: "back", name: "接口讨论", members: ["Lin"] },
    { group_id: "other", name: "跨项目讨论", members: ["Ada"] },
  ] }),
  useGetRoutinesQuery: (sessionId: string) => ({ currentData: sessionId !== "ada" ? [] : [
    { routine_id: "r-pr", name: "检查 PR", intent: "看评审", schedule: "@every 10m", state: "active", next_run: null, last_run: null },
  ] }),
  useSetRoutineStateMutation: () => [() => undefined, { isLoading: false }],
  useGetBackgroundCommandsQuery: (sessionId: string) => ({ currentData: sessionId !== "ada" ? [] : [
    { command_id: "download", command: "download release.zip", state: "running", duration_ms: 65000, exit_code: null },
  ], refetch: commands.refetch }),
  useStopBackgroundCommandMutation: () => [commands.stop, { isLoading: false }],
  useLinkProjectMutation: () => [() => undefined, { isLoading: false, reset: () => undefined }],
  useSetAgentAvatarMutation: () => [() => undefined, { isLoading: false }],
  useUploadAgentAvatarMutation: () => [() => undefined, { isLoading: false }],
  useRenameAgentMutation: () => [() => undefined, { isLoading: false }],
  useUnlinkProjectMutation: () => [() => undefined, { isLoading: false }],
}));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("桌面资料默认展开，后台任务与例行任务跟随当前 Agent，也可手动收起", async () => {
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  const store = configureStore({ reducer: { runtime: runtimeReducer } });
  store.dispatch(connected("connection"));
  render(<Provider store={store}><MantineProvider env="test"><MemoryRouter initialEntries={["/sessions/ada"]}>
    <Link to="/sessions/lin">切换到 Lin</Link>
    <Routes><Route path="/sessions/:sessionId" element={<SessionConversation />} /></Routes>
  </MemoryRouter></MantineProvider></Provider>);
  expect(screen.getByRole("button", { name: "收起 Bot 资料" })).toHaveAttribute("aria-expanded", "true");
  const details = within(screen.getByRole("complementary", { name: "Agent 资料" }));
  expect(details.getByText("Alpha")).toBeInTheDocument();
  expect(details.getByRole("link", { name: "界面讨论" })).toHaveAttribute("href", "/groups/front");
  expect(details.getByText("跨项目讨论")).toBeInTheDocument();
  expect(details.getByText("检查 PR")).toBeInTheDocument();
  const background = within(details.getByRole("region", { name: "后台任务" }));
  expect(background.getByText("download release.zip")).toBeVisible();
  expect(background.getByText("1 项运行中")).toBeVisible();
  expect(background.getByText("1 分 5 秒")).toBeVisible();
  fireEvent.click(background.getByRole("button", { name: "停止命令" }));
  expect(commands.stop).toHaveBeenCalledWith({ sessionId: "ada", connectionId: "connection", commandId: "download" });
  await Promise.resolve();
  expect(commands.refetch).toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "收起 Bot 资料" }));
  expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
  expect(screen.getByText("对话内容")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "显示 Bot 资料" }));
  fireEvent.click(screen.getByRole("link", { name: "切换到 Lin" }));
  const next = within(screen.getByRole("complementary", { name: "Agent 资料" }));
  expect(next.getByText("Beta")).toBeInTheDocument();
  expect(next.queryByText("Alpha")).not.toBeInTheDocument();
  expect(next.getByRole("link", { name: "接口讨论" })).toBeInTheDocument();
  expect(next.getByText("无例行任务")).toBeInTheDocument();
  expect(next.getByText("无后台任务")).toBeInTheDocument();
  expect(next.queryByText("download release.zip")).not.toBeInTheDocument();
});

it("窄屏从页头打开资料后仍能访问例行任务，并能关闭回到对话", () => {
  vi.stubGlobal("matchMedia", () => ({ matches: true, addEventListener() {}, removeEventListener() {} }));
  const store = configureStore({ reducer: { runtime: runtimeReducer } });
  render(<Provider store={store}><MantineProvider env="test"><MemoryRouter initialEntries={["/sessions/ada"]}>
    <Routes><Route path="/sessions/:sessionId" element={<SessionConversation />} /></Routes>
  </MemoryRouter></MantineProvider></Provider>);
  expect(screen.queryByRole("dialog", { name: "Agent 资料" })).not.toBeInTheDocument();
  expect(screen.getByText("对话内容")).toBeVisible();
  fireEvent.click(within(screen.getByRole("banner")).getByRole("button", { name: "显示 Bot 资料" }));
  const details = within(screen.getByRole("dialog", { name: "Agent 资料" }));
  expect(details.getByRole("button", { name: "查看 检查 PR" })).toBeVisible();
  expect(details.getByRole("switch", { name: "暂停 检查 PR" })).toBeVisible();
  expect(details.getByRole("button", { name: "删除 检查 PR" })).toBeVisible();
  expect(details.getByText("download release.zip")).toBeVisible();
  fireEvent.click(details.getByRole("button", { name: "关闭 Agent 资料" }));
  expect(screen.queryByRole("dialog", { name: "Agent 资料" })).not.toBeInTheDocument();
  expect(screen.getByText("对话内容")).toBeVisible();
});
