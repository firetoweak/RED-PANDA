import { MantineProvider } from "@mantine/core";
import { configureStore } from "@reduxjs/toolkit";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { Provider } from "react-redux";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import type { GroupView } from "../src/api/contracts";
import { GroupChatPage } from "../src/features/groups/GroupChatPage";
import runtimeReducer, { connected } from "../src/realtime/runtimeSlice";

const api = vi.hoisted(() => ({
  group: undefined as GroupView | undefined,
  post: vi.fn(() => ({ unwrap: () => Promise.resolve() })),
}));
vi.mock("../src/api/redpandaApi", () => ({
  useGetGroupQuery: () => ({ data: api.group, error: undefined }),
  useGetWorkspacesQuery: () => ({ data: [{ workspace_id: "workspace", name: "当前项目", task_root: "/project" }] }),
  useGetAgentsQuery: () => ({ data: [{
    session_id: "other-ada", workspace_id: "other", name: "Ada", direction: "其他项目", activity: "idle", status: "idle",
  }, {
    session_id: "agent-ada", workspace_id: "workspace", name: "Ada", direction: null, activity: "idle", status: "idle",
  }] }),
  usePostGroupMessageMutation: () => [api.post, { isLoading: false }],
}));

function group(partial: Partial<GroupView>): GroupView {
  return {
    group_id: "group-1", workspace_id: "workspace", name: "开发组", members: ["Ada", "Lin"],
    calling: [], messages: [], ...partial,
  };
}

function renderPage() {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  Object.defineProperty(document, "fonts", { configurable: true, value: { addEventListener() {}, removeEventListener() {} } });
  const store = configureStore({ reducer: { runtime: runtimeReducer } });
  store.dispatch(connected("connection"));
  return render(
    <Provider store={store}>
      <MantineProvider env="test">
        <MemoryRouter initialEntries={["/groups/group-1"]}>
          <Routes><Route path="/groups/:groupId" element={<GroupChatPage />} /></Routes>
        </MemoryRouter>
      </MantineProvider>
    </Provider>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  Reflect.deleteProperty(document, "fonts");
  api.post.mockClear();
});

it("群记录区分人和 Agent，显示点名与正在等谁回应", () => {
  api.group = group({
    calling: ["Lin"],
    messages: [
      { message_id: "m1", sender: null, text: "大家同步一下", mentioned_agents: [], reply_to: null,
        occurred_at: "2026-10-18T10:00:00+00:00" },
      { message_id: "m2", sender: "Ada", text: "接口改好了", mentioned_agents: ["Lin"], reply_to: null,
        occurred_at: "2026-10-18T10:01:00+00:00" },
    ],
  });
  renderPage();
  expect(screen.getByText("你")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Ada" })).toHaveAttribute("href", "/sessions/agent-ada");
  const details = within(screen.getByRole("complementary", { name: "群资料" }));
  expect(details.getByRole("link", { name: "查看 Ada 的会话" })).toHaveAttribute("href", "/sessions/agent-ada");
  expect(details.queryByText("其他项目")).not.toBeInTheDocument();
  expect(details.getByRole("link", { name: "当前项目" })).toHaveAttribute("href", "/workspaces/workspace");
  fireEvent.click(screen.getByRole("button", { name: "收起群资料" }));
  expect(screen.queryByRole("complementary", { name: "群资料" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "显示群资料" }));
  expect(screen.getByRole("complementary", { name: "群资料" })).toBeInTheDocument();
  expect(screen.getByText("@Lin")).toBeInTheDocument();
  expect(screen.getByText("正在等 Lin 回应…")).toBeInTheDocument();
});

it("发言带上点名", async () => {
  api.group = group({});
  renderPage();
  fireEvent.change(screen.getByLabelText("群消息"), { target: { value: "@Li" } });
  expect(screen.getByRole("option", { name: /Lin/ })).toBeInTheDocument();
  fireEvent.keyDown(screen.getByLabelText("群消息"), { key: "Enter" });
  expect(screen.getByLabelText("群消息")).toHaveValue("@Lin ");
  expect(api.post).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText("群消息"), { target: { value: "@Lin 看看测试" } });
  fireEvent.keyDown(screen.getByLabelText("群消息"), { key: "Enter", shiftKey: true });
  fireEvent.keyDown(screen.getByLabelText("群消息"), { key: "Enter", isComposing: true });
  expect(api.post).not.toHaveBeenCalled();
  fireEvent.keyDown(screen.getByLabelText("群消息"), { key: "Enter" });
  await waitFor(() => expect(api.post).toHaveBeenCalledWith({
    connectionId: "connection", groupId: "group-1", text: "@Lin 看看测试", mention: ["Lin"],
  }));
});

it("候选支持方向键和点选，删除正文点名后不保留收件人", async () => {
  api.group = group({});
  renderPage();
  const input = screen.getByLabelText("群消息");
  fireEvent.click(screen.getByRole("button", { name: "点名成员" }));
  expect(input).toHaveValue("@");
  fireEvent.keyDown(input, { key: "ArrowDown" });
  expect(screen.getByRole("option", { name: /Lin/ })).toHaveAttribute("aria-selected", "true");
  fireEvent.keyDown(input, { key: "Tab" });
  expect(input).toHaveValue("@Lin ");
  fireEvent.change(input, { target: { value: "@Lin @Ad" } });
  fireEvent.click(screen.getByRole("option", { name: /Ada/ }));
  expect(input).toHaveValue("@Lin @Ada ");
  fireEvent.change(input, { target: { value: "大家同步一下" } });
  expect(screen.getByText("未点名 · 通知全体")).toBeInTheDocument();
  fireEvent.keyDown(input, { key: "Enter" });
  await waitFor(() => expect(api.post).toHaveBeenCalledWith({
    connectionId: "connection", groupId: "group-1", text: "大家同步一下", mention: [],
  }));
});

it("完整名字决定收件人，支持中文、带空格和符号的名字，并排除邮箱", async () => {
  api.group = group({ members: ["Ada", "Ada QA", "李雷", "C++"] });
  renderPage();
  const input = screen.getByLabelText("群消息");
  fireEvent.change(input, { target: { value: "请@李" } });
  fireEvent.compositionStart(input);
  fireEvent.keyDown(input, { key: "Enter" });
  expect(api.post).not.toHaveBeenCalled();
  fireEvent.compositionEnd(input);
  fireEvent.click(screen.getByRole("option", { name: /李雷/ }));
  expect(input).toHaveValue("请@李雷 ");
  fireEvent.change(input, { target: { value: "请@李雷 同步 @Ada QA 和 @C++，联系 dev@Ada。" } });
  fireEvent.click(screen.getByRole("button", { name: "发送到群" }));
  await waitFor(() => expect(api.post).toHaveBeenCalledWith({
    connectionId: "connection", groupId: "group-1", text: "请@李雷 同步 @Ada QA 和 @C++，联系 dev@Ada。", mention: ["Ada QA", "李雷", "C++"],
  }));
});
