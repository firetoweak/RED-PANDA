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
    { workspace_id: "alpha", group_id: "front", name: "界面讨论", members: ["Ada"] },
    { workspace_id: "beta", group_id: "back", name: "接口讨论", members: ["Lin"] },
    { workspace_id: "beta", group_id: "other", name: "跨项目讨论", members: ["Ada"] },
  ] }),
}));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("右侧资料跟随当前 Agent，按项目和成员筛选群聊，收起后可重新打开", () => {
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  const store = configureStore({ reducer: { runtime: runtimeReducer } });
  render(<Provider store={store}><MantineProvider env="test"><MemoryRouter initialEntries={["/sessions/ada"]}>
    <Link to="/sessions/lin">切换到 Lin</Link>
    <Routes><Route path="/sessions/:sessionId" element={<SessionConversation />} /></Routes>
  </MemoryRouter></MantineProvider></Provider>);
  const details = within(screen.getByRole("complementary", { name: "Agent 资料" }));
  expect(details.getByText("前端体验")).toBeInTheDocument();
  expect(details.getByText("Alpha")).toBeInTheDocument();
  expect(details.getByRole("link", { name: "界面讨论" })).toHaveAttribute("href", "/groups/front");
  expect(details.getByText("跨项目讨论")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "收起 Agent 资料" }));
  expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
  expect(screen.getByText("对话内容")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "显示 Agent 资料" }));
  fireEvent.click(screen.getByRole("link", { name: "切换到 Lin" }));
  const next = within(screen.getByRole("complementary", { name: "Agent 资料" }));
  expect(next.getByText("接口验证")).toBeInTheDocument();
  expect(next.getByText("Beta")).toBeInTheDocument();
  expect(next.queryByText("前端体验")).not.toBeInTheDocument();
  expect(next.getByRole("link", { name: "接口讨论" })).toBeInTheDocument();
});
