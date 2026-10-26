import { MantineProvider } from "@mantine/core";
import { configureStore } from "@reduxjs/toolkit";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useImperativeHandle, type Ref } from "react";
import { Provider } from "react-redux";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { BackgroundCommands } from "../src/features/conversation/BackgroundCommands";
import { BotDetailsPanel } from "../src/features/conversation/BotDetailsPanel";
import { SessionConversation } from "../src/features/conversation/SessionConversation";
import { SubagentList } from "../src/features/conversation/SubagentList";
import runtimeReducer, { connected } from "../src/realtime/runtimeSlice";

const commands = vi.hoisted(() => ({
  stop: vi.fn(() => Promise.resolve({ data: {} })), refetch: vi.fn(), state: "running", isError: false,
}));
const subagents = vi.hoisted(() => ({
  observe: vi.fn(),
  isError: false,
  isLoading: false,
  pendingSessionId: null as string | null,
  items: [
    { command_id: "create-child", child_session_id: "ada/child-create", task: "准备检查服务", status: "creating" },
    { command_id: "review-child", child_session_id: "ada/child-review", task: "检查登录页面", status: "running" },
    { command_id: "wait-child", child_session_id: "ada/child-wait", task: "补充接口证据", status: "waiting" },
    { command_id: "closed-child", child_session_id: "ada/child-closed", task: "已验收的检查", status: "closed" },
    { command_id: "failed-child", child_session_id: "ada/child-failed", task: "无法启动的检查", status: "failed" },
  ],
}));

vi.mock("../src/features/conversation/Conversation", () => ({
  Conversation: ({ ref }: { ref?: Ref<{ observeSubagent: (commandId: string) => void }> }) => {
    useImperativeHandle(ref, () => ({ observeSubagent: subagents.observe }), []);
    return <p>对话内容</p>;
  },
}));
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
  useGetRoutineFiresQuery: () => ({ currentData: [] }),
  useGetSubagentsQuery: (sessionId: string) => ({
    currentData: subagents.pendingSessionId === sessionId ? undefined : sessionId === "ada" ? subagents.items : [],
    data: subagents.items,
    isError: subagents.isError,
    isLoading: subagents.isLoading,
  }),
  useSetRoutineStateMutation: () => [() => undefined, { isLoading: false }],
  useGetBackgroundCommandsQuery: (sessionId: string) => ({ currentData: [
    ...(sessionId !== "ada" ? [] : [{ command_id: "download", command: "download release.zip", state: commands.state, duration_ms: 65000, exit_code: null }]),
    ...["completed", "stopped", "unknown"].map((state) => ({ command_id: state, command: `${state} command`, state, duration_ms: 2000, exit_code: 0 })),
  ], refetch: commands.refetch, isError: commands.isError }),
  useStopBackgroundCommandMutation: () => [commands.stop, { isLoading: false }],
  useLinkProjectMutation: () => [() => undefined, { isLoading: false, reset: () => undefined }],
  useSetAgentAvatarMutation: () => [() => undefined, { isLoading: false }],
  useUploadAgentAvatarMutation: () => [() => undefined, { isLoading: false }],
  useRenameAgentMutation: () => [() => undefined, { isLoading: false }],
  useUnlinkProjectMutation: () => [() => undefined, { isLoading: false }],
}));
beforeEach(() => {
  vi.clearAllMocks();
  commands.state = "running";
  commands.isError = false;
  subagents.isError = false;
  subagents.isLoading = false;
  subagents.pendingSessionId = null;
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); window.localStorage.clear(); });

it("桌面资料默认详情，页签往返保留例行任务打开项，切 Bot 回到详情且不串旧数据", async () => {
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  const store = configureStore({ reducer: { runtime: runtimeReducer } });
  store.dispatch(connected("connection"));
  render(<Provider store={store}><MantineProvider env="test"><MemoryRouter initialEntries={["/sessions/ada"]}>
    <Link to="/sessions/lin">切换到 Lin</Link>
    <Routes><Route path="/sessions/:sessionId" element={<SessionConversation />} /></Routes>
  </MemoryRouter></MantineProvider></Provider>);
  expect(screen.getByRole("button", { name: "收起 Bot 资料" })).toHaveAttribute("aria-expanded", "true");
  const details = within(screen.getByRole("complementary", { name: "Agent 资料" }));
  expect(details.getByRole("tab", { name: "详情" })).toHaveAttribute("aria-selected", "true");
  expect(details.getByText("Alpha")).toBeInTheDocument();
  expect(details.getByRole("link", { name: "界面讨论" })).toHaveAttribute("href", "/groups/front");
  expect(details.getByText("跨项目讨论")).toBeInTheDocument();
  expect(details.queryByRole("button", { name: "查看 检查 PR" })).not.toBeInTheDocument();
  const background = within(details.getByRole("region", { name: "后台任务" }));
  expect(background.getByText("download release.zip")).toBeVisible();
  expect(background.getByText("1 项运行中")).toBeVisible();
  expect(background.getByText("1 分 5 秒")).toBeVisible();
  const children = within(details.getByRole("region", { name: "子智能体" }));
  expect(children.getByText("检查登录页面")).toBeVisible();
  fireEvent.click(children.getByRole("button", { name: "查看子智能体过程：检查登录页面" }));
  expect(subagents.observe).toHaveBeenCalledWith("review-child");
  for (const state of ["completed", "stopped", "unknown"]) expect(background.queryByText(`${state} command`)).not.toBeInTheDocument();
  fireEvent.click(background.getByRole("button", { name: "停止命令" }));
  expect(commands.stop).toHaveBeenCalledWith({ sessionId: "ada", connectionId: "connection", commandId: "download" });
  await waitFor(() => expect(commands.refetch).toHaveBeenCalled());
  fireEvent.click(details.getByRole("tab", { name: "例行任务" }));
  expect(details.getByText("Ada")).toBeVisible();
  fireEvent.click(details.getByRole("button", { name: "查看 检查 PR" }));
  expect(details.getByRole("button", { name: "返回例行任务" })).toBeVisible();
  expect(details.getByText("看评审")).toBeVisible();
  fireEvent.click(details.getByRole("tab", { name: "详情" }));
  expect(background.getByText("download release.zip")).toBeVisible();
  fireEvent.click(details.getByRole("tab", { name: "例行任务" }));
  expect(details.getByRole("button", { name: "返回例行任务" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "收起 Bot 资料" }));
  expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
  expect(screen.getByText("对话内容")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "显示 Bot 资料" }));
  fireEvent.click(screen.getByRole("tab", { name: "例行任务" }));
  expect(screen.getByRole("tab", { name: "例行任务" })).toHaveAttribute("aria-selected", "true");
  subagents.pendingSessionId = "lin";
  fireEvent.click(screen.getByRole("link", { name: "切换到 Lin" }));
  const next = within(screen.getByRole("complementary", { name: "Agent 资料" }));
  await waitFor(() => expect(next.getByRole("tab", { name: "详情" })).toHaveAttribute("aria-selected", "true"));
  expect(next.getByText("Beta")).toBeInTheDocument();
  expect(next.queryByText("Alpha")).not.toBeInTheDocument();
  expect(next.getByRole("link", { name: "接口讨论" })).toBeInTheDocument();
  expect(next.getByText("无运行中的后台任务")).toBeInTheDocument();
  expect(next.queryByText("download release.zip")).not.toBeInTheDocument();
  expect(next.queryByText("检查登录页面")).not.toBeInTheDocument();
  expect(next.queryByText("补充接口证据")).not.toBeInTheDocument();
  fireEvent.click(next.getByRole("tab", { name: "例行任务" }));
  expect(next.getByText("无例行任务")).toBeVisible();
  expect(next.queryByRole("button", { name: "返回例行任务" })).not.toBeInTheDocument();
});

it("窄屏资料提供相同的详情、子过程和例行任务入口，关闭后回到对话", () => {
  vi.stubGlobal("matchMedia", () => ({ matches: true, addEventListener() {}, removeEventListener() {} }));
  const store = configureStore({ reducer: { runtime: runtimeReducer } });
  render(<Provider store={store}><MantineProvider env="test"><MemoryRouter initialEntries={["/sessions/ada"]}>
    <Routes><Route path="/sessions/:sessionId" element={<SessionConversation />} /></Routes>
  </MemoryRouter></MantineProvider></Provider>);
  expect(screen.queryByRole("dialog", { name: "Agent 资料" })).not.toBeInTheDocument();
  expect(screen.getByText("对话内容")).toBeVisible();
  fireEvent.click(within(screen.getByRole("banner")).getByRole("button", { name: "显示 Bot 资料" }));
  const details = within(screen.getByRole("dialog", { name: "Agent 资料" }));
  expect(details.getByRole("tab", { name: "详情" })).toHaveAttribute("aria-selected", "true");
  expect(details.getByText("download release.zip")).toBeVisible();
  expect(details.getByRole("region", { name: "子智能体" })).toBeVisible();
  expect(details.getByText("检查登录页面")).toBeVisible();
  fireEvent.click(details.getByRole("button", { name: "查看子智能体过程：检查登录页面" }));
  expect(subagents.observe).toHaveBeenCalledWith("review-child");
  expect(screen.queryByRole("dialog", { name: "Agent 资料" })).not.toBeInTheDocument();
  fireEvent.click(within(screen.getByRole("banner")).getByRole("button", { name: "显示 Bot 资料" }));
  const reopened = within(screen.getByRole("dialog", { name: "Agent 资料" }));
  fireEvent.click(reopened.getByRole("tab", { name: "例行任务" }));
  expect(reopened.getByText("Ada")).toBeVisible();
  expect(reopened.getByRole("button", { name: "查看 检查 PR" })).toBeVisible();
  expect(reopened.getByRole("switch", { name: "暂停 检查 PR" })).toBeVisible();
  expect(reopened.getByRole("button", { name: "删除 检查 PR" })).toBeVisible();
  fireEvent.click(reopened.getByRole("button", { name: "关闭 Agent 资料" }));
  expect(screen.queryByRole("dialog", { name: "Agent 资料" })).not.toBeInTheDocument();
  expect(screen.getByText("对话内容")).toBeVisible();
  expect(screen.queryByRole("separator", { name: "调节 Bot 资料宽度" })).not.toBeInTheDocument();
});

it("子智能体列表呈现实际协作状态，所有状态的过程入口都使用委派 command_id", () => {
  const onObserve = vi.fn();
  render(<MantineProvider env="test"><SubagentList sessionId="ada" onObserve={onObserve} /></MantineProvider>);
  const list = within(screen.getByRole("region", { name: "子智能体" }));
  for (const label of ["创建中", "运行中", "等待中", "已结束", "失败"]) {
    expect(list.getByText(label)).toBeVisible();
  }
  for (const item of subagents.items) {
    expect(list.getByText(item.task)).toBeVisible();
    fireEvent.click(list.getByRole("button", { name: `查看子智能体过程：${item.task}` }));
    expect(onObserve).toHaveBeenLastCalledWith(item.command_id);
  }
  expect(onObserve).toHaveBeenCalledTimes(subagents.items.length);
});

it("子列表错误时不显示缓存任务，切到无子任务的 Bot 显示空态", () => {
  subagents.isError = true;
  const show = (sessionId: string) => <MantineProvider env="test"><SubagentList sessionId={sessionId} onObserve={() => {}} /></MantineProvider>;
  const view = render(show("ada"));
  expect(screen.getByRole("alert")).toBeVisible();
  expect(screen.queryByText("检查登录页面")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "查看子智能体过程：检查登录页面" })).not.toBeInTheDocument();
  subagents.isError = false;
  view.rerender(show("lin"));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.getByText("无子智能体")).toBeVisible();
  expect(screen.queryByText("检查登录页面")).not.toBeInTheDocument();
});

it("只能从列表删除结束或失败的子智能体，重新打开后保持删除且仍可观察进行中的协作", () => {
  const onObserve = vi.fn();
  const show = () => <MantineProvider env="test"><SubagentList sessionId="ada" onObserve={onObserve} /></MantineProvider>;
  const view = render(show());
  const list = within(screen.getByRole("region", { name: "子智能体" }));
  expect(list.getAllByRole("button", { name: /^删除子智能体：/ })).toHaveLength(2);
  fireEvent.click(list.getByRole("button", { name: "删除子智能体：已验收的检查" }));
  fireEvent.click(list.getByRole("button", { name: "删除子智能体：无法启动的检查" }));
  expect(list.getByText("3 项")).toBeVisible();
  expect(list.queryByText("已验收的检查")).not.toBeInTheDocument();
  expect(list.queryByText("无法启动的检查")).not.toBeInTheDocument();
  view.unmount();
  render(show());
  const reopened = within(screen.getByRole("region", { name: "子智能体" }));
  expect(reopened.getByText("3 项")).toBeVisible();
  expect(reopened.queryByRole("button", { name: /^删除子智能体：/ })).not.toBeInTheDocument();
  for (const task of ["准备检查服务", "检查登录页面", "补充接口证据"]) {
    expect(reopened.getByText(task)).toBeVisible();
    fireEvent.click(reopened.getByRole("button", { name: `查看子智能体过程：${task}` }));
  }
  expect(onObserve).toHaveBeenCalledTimes(3);
});

it("后台列表读取失败时不误报为空，也不提供旧任务的停止入口", () => {
  commands.isError = true;
  render(<MantineProvider env="test"><BackgroundCommands sessionId="ada" connectionId="connection" /></MantineProvider>);
  expect(screen.getByRole("alert")).toHaveTextContent("后台任务读取失败");
  expect(screen.queryByText("无运行中的后台任务")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "停止命令" })).not.toBeInTheDocument();
});

it("后台任务更新为结束或停止后从当前列表移除", () => {
  const view = render(<MantineProvider env="test"><BackgroundCommands sessionId="ada" connectionId="connection" /></MantineProvider>);
  expect(screen.getByText("download release.zip")).toBeVisible();
  for (const state of ["completed", "stopped", "unknown"]) {
    commands.state = state;
    view.rerender(<MantineProvider env="test"><BackgroundCommands sessionId="ada" connectionId="connection" /></MantineProvider>);
    expect(screen.queryByText("download release.zip")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "停止命令" })).not.toBeInTheDocument();
    expect(screen.getByText("无运行中的后台任务")).toBeVisible();
  }
});

it("资料栏向左拖宽、向右拖窄，保留聊天空间，关闭重开记住宽度并可用键盘调整", () => {
  vi.stubGlobal("PointerEvent", class extends MouseEvent {
    pointerId: number;
    constructor(type: string, init: PointerEventInit) { super(type, init); this.pointerId = init.pointerId ?? 1; }
  });
  let parentWidth = 1000;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    const width = this.classList.contains("bot-details-panel") ? Number.parseFloat(this.style.getPropertyValue("--bot-details-width")) : parentWidth;
    return { width, right: 1200 } as DOMRect;
  });
  const panel = () => <MantineProvider env="test"><div><BotDetailsPanel onClose={() => undefined}><p>资料内容</p></BotDetailsPanel></div></MantineProvider>;
  const view = render(panel());
  let handle = screen.getByRole("separator", { name: "调节 Bot 资料宽度" });
  Object.assign(handle, { setPointerCapture: vi.fn() });
  fireEvent.pointerDown(handle, { button: 0, pointerId: 1 });
  fireEvent.pointerMove(handle, { clientX: 800, pointerId: 1 });
  expect(handle).toHaveAttribute("aria-valuenow", "400");
  fireEvent.pointerMove(handle, { clientX: 1000, pointerId: 2 });
  expect(handle).toHaveAttribute("aria-valuenow", "400");
  fireEvent.pointerUp(handle, { pointerId: 1 });
  expect(window.localStorage.getItem("konden.botDetailsWidth")).toBe("400");
  view.unmount();
  expect(document.body).not.toHaveClass("is-resizing-bot-details");
  render(panel());
  handle = screen.getByRole("separator", { name: "调节 Bot 资料宽度" });
  expect(handle).toHaveAttribute("aria-valuenow", "400");
  fireEvent.keyDown(handle, { key: "ArrowRight" });
  expect(window.localStorage.getItem("konden.botDetailsWidth")).toBe("380");
  Object.assign(handle, { setPointerCapture: vi.fn() });
  fireEvent.pointerDown(handle, { button: 0, pointerId: 1 });
  fireEvent.pointerMove(handle, { clientX: 1199, pointerId: 1 });
  expect(handle).toHaveAttribute("aria-valuenow", "220");
  parentWidth = 900;
  fireEvent.pointerMove(handle, { clientX: 0, pointerId: 1 });
  expect(handle).toHaveAttribute("aria-valuenow", "420");
  fireEvent.pointerCancel(handle, { pointerId: 1 });
  expect(document.body).not.toHaveClass("is-resizing-bot-details");
  expect(window.localStorage.getItem("konden.botDetailsWidth")).toBe("420");
});
