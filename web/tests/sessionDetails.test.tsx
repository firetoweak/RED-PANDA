import { MantineProvider } from "@mantine/core";
import { configureStore } from "@reduxjs/toolkit";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { Provider } from "react-redux";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { SessionConversation } from "../src/features/conversation/SessionConversation";
import runtimeReducer from "../src/realtime/runtimeSlice";

vi.mock("../src/features/conversation/Conversation", () => ({ Conversation: () => <p>对话内容</p> }));
vi.mock("../src/api/redpandaApi", () => ({
  useGetAgentsQuery: () => ({ data: [
    { session_id: "ada", workspace_id: "alpha", name: "Ada", direction: "前端体验", status: "waiting" },
    { session_id: "lin", workspace_id: "beta", name: "Lin", direction: "接口验证", status: "idle" },
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
}));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("资料默认隐藏，点击页头名字打开和收起，资料与例行任务跟随当前 Agent", () => {
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  const store = configureStore({ reducer: { runtime: runtimeReducer } });
  render(<Provider store={store}><MantineProvider env="test"><MemoryRouter initialEntries={["/sessions/ada"]}>
    <Link to="/sessions/lin">切换到 Lin</Link>
    <Routes><Route path="/sessions/:sessionId" element={<SessionConversation />} /></Routes>
  </MemoryRouter></MantineProvider></Provider>);
  expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
  const header = within(screen.getByRole("banner"));
  expect(header.getByRole("button", { name: "Ada" })).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(header.getByRole("button", { name: "Ada" }));
  const details = within(screen.getByRole("complementary", { name: "Agent 资料" }));
  expect(details.getByText("前端体验")).toBeInTheDocument();
  expect(details.getByText("Alpha")).toBeInTheDocument();
  expect(details.getByRole("link", { name: "界面讨论" })).toHaveAttribute("href", "/groups/front");
  expect(details.getByText("跨项目讨论")).toBeInTheDocument();
  expect(details.getByText("检查 PR")).toBeInTheDocument();
  fireEvent.click(header.getByRole("button", { name: "Ada" }));
  expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
  expect(screen.getByText("对话内容")).toBeInTheDocument();
  fireEvent.click(header.getByRole("button", { name: "Ada" }));
  fireEvent.click(screen.getByRole("link", { name: "切换到 Lin" }));
  const next = within(screen.getByRole("complementary", { name: "Agent 资料" }));
  expect(next.getByText("接口验证")).toBeInTheDocument();
  expect(next.getByText("Beta")).toBeInTheDocument();
  expect(next.queryByText("前端体验")).not.toBeInTheDocument();
  expect(next.getByRole("link", { name: "接口讨论" })).toBeInTheDocument();
  expect(next.getByText("无例行任务")).toBeInTheDocument();
});
