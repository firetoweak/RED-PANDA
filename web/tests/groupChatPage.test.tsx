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
  post: vi.fn((_args: { deliveryId: string }) => ({ unwrap: () => Promise.resolve() })),
}));
vi.mock("../src/api/redpandaApi", () => ({
  useGetGroupQuery: () => ({ data: api.group, error: undefined }),
  useGetWorkspacesQuery: () => ({ data: [{ workspace_id: "workspace", name: "当前项目", task_root: "/project" }] }),
  useGetAgentsQuery: () => ({ data: [{
    session_id: "other-ada", workspace_id: "other", name: "Other", activity: "idle", status: "idle",
  }, {
    session_id: "agent-ada", workspace_id: "workspace", name: "Ada", activity: "idle", status: "idle",
  }] }),
  usePostGroupMessageMutation: () => [api.post, { isLoading: false }],
}));

function group(partial: Partial<GroupView>): GroupView {
  return {
    group_id: "group-1", name: "开发组", members: ["Ada", "Lin"],
    dissolved: false, calling: [], messages: [], ...partial,
  };
}

function renderPage(groupId = "group-1") {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  Object.defineProperty(document, "fonts", { configurable: true, value: { addEventListener() {}, removeEventListener() {} } });
  const store = configureStore({ reducer: { runtime: runtimeReducer } });
  store.dispatch(connected("connection"));
  return render(
    <Provider store={store}>
      <MantineProvider env="test">
        <MemoryRouter initialEntries={[`/groups/${groupId}`]}>
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
  localStorage.clear();
});

it("群记录区分人和 Agent，显示点名与正在通知的成员", () => {
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
  expect(within(screen.getByRole("article", { name: "你的群消息" })).getAllByText("你").length).toBeGreaterThan(0);
  expect(screen.getByRole("link", { name: "Ada" })).toHaveAttribute("href", "/sessions/agent-ada");
  expect(screen.queryByRole("complementary", { name: "群资料" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "显示群资料" }));
  const details = within(screen.getByRole("complementary", { name: "群资料" }));
  expect(details.getByRole("link", { name: "查看 Ada 的会话" })).toHaveAttribute("href", "/sessions/agent-ada");
  expect(details.queryByText("其他项目")).not.toBeInTheDocument();
  expect(details.queryByText("当前项目")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "收起群资料" }));
  expect(screen.queryByRole("complementary", { name: "群资料" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "显示群资料" }));
  expect(screen.getByRole("complementary", { name: "群资料" })).toBeInTheDocument();
  expect(screen.getByText("@Lin")).toBeInTheDocument();
  expect(screen.getByText("正在通知 Lin…")).toBeInTheDocument();
});

it("解散的群保留消息，只读查看", () => {
  api.group = group({ dissolved: true, messages: [{ message_id: "history", sender: "Ada", text: "已完成的成果",
    mentioned_agents: [], reply_to: null, occurred_at: "2026-10-18T10:00:00Z" }] });
  renderPage();
  expect(screen.getByText("已完成的成果")).toBeInTheDocument();
  expect(screen.getByText("群聊已解散，此处仅保留聊天记录。")).toBeInTheDocument();
  expect(screen.queryByLabelText("群消息")).not.toBeInTheDocument();
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
    connectionId: "connection", groupId: "group-1", deliveryId: expect.any(String), text: "@Lin 看看测试", mention: ["Lin"],
  }));
});

it("候选支持方向键和点选，删除正文点名后不保留收件人", async () => {
  api.group = group({});
  renderPage();
  const input = screen.getByLabelText("群消息");
  fireEvent.click(screen.getByRole("button", { name: "点名成员" }));
  expect(input).toHaveValue("@");
  fireEvent.keyDown(input, { key: "ArrowDown" });
  fireEvent.keyDown(input, { key: "ArrowDown" });
  expect(screen.getByRole("option", { name: /Lin/ })).toHaveAttribute("aria-selected", "true");
  fireEvent.keyDown(input, { key: "Tab" });
  expect(input).toHaveValue("@Lin ");
  fireEvent.change(input, { target: { value: "@Lin @Ad" } });
  fireEvent.click(screen.getByRole("option", { name: /Ada/ }));
  expect(input).toHaveValue("@Lin @Ada ");
  fireEvent.change(input, { target: { value: "大家同步一下" } });
  expect(screen.getByText("群消息 · 点名可选")).toBeInTheDocument();
  fireEvent.keyDown(input, { key: "Enter" });
  await waitFor(() => expect(api.post).toHaveBeenCalledWith({
    connectionId: "connection", groupId: "group-1", deliveryId: expect.any(String), text: "大家同步一下", mention: [],
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
    connectionId: "connection", groupId: "group-1", deliveryId: expect.any(String), text: "请@李雷 同步 @Ada QA 和 @C++，联系 dev@Ada。", mention: ["Ada QA", "李雷", "C++"],
  }));
});

it("整条引用保留来源但不把预览中的点名当作通知", async () => {
  api.group = group({ messages: [{ message_id: "source", sender: "Ada", text: "@all @Lin 看看这份完整报告", mentioned_agents: ["Lin"], reply_to: null, occurred_at: "2026-10-18T10:00:00Z" }] });
  renderPage();
  fireEvent.contextMenu(screen.getByRole("article", { name: "Ada的群消息" }).querySelector(".group-message-bubble")!, { clientX: 100, clientY: 100 });
  fireEvent.click(await screen.findByRole("menuitem", { name: "引用 Ada 的消息" }));
  await screen.findByRole("button", { name: "取消引用" });
  expect(screen.getByLabelText("群消息")).toHaveValue("");
  fireEvent.change(screen.getByLabelText("群消息"), { target: { value: "这个结论继续讨论" } });
  fireEvent.click(screen.getByRole("button", { name: "发送到群" }));
  await waitFor(() => expect(api.post).toHaveBeenCalledWith({ connectionId: "connection", groupId: "group-1", deliveryId: expect.any(String), text: "> [Ada](#message:source)\n> @all @Lin 看看这份完整报告\n\n这个结论继续讨论", mention: [] }));
});

it("全体候选置顶，补全后通知全部成员且不增加输入区提示", async () => {
  api.group = group({});
  renderPage();
  const input = screen.getByLabelText("群消息");
  fireEvent.click(screen.getByRole("button", { name: "点名成员" }));
  expect(screen.getAllByRole("option")[0]).toHaveAccessibleName(/所有人\s*@all/);
  fireEvent.keyDown(input, { key: "Enter" });
  expect(input).toHaveValue("@all ");
  expect(api.post).not.toHaveBeenCalled();
  expect(screen.getByText("群消息 · 点名可选")).toBeInTheDocument();
  fireEvent.change(input, { target: { value: "@all @Ada 大家同步一下" } });
  fireEvent.click(screen.getByRole("button", { name: "发送到群" }));
  await waitFor(() => expect(api.post).toHaveBeenCalledWith({
    connectionId: "connection", groupId: "group-1", deliveryId: expect.any(String), text: "@all @Ada 大家同步一下", mention: [],
  }));
});

it.each([
  ["@ALL，同步一下 @Ada", []],
  ["dev@all.example @Ada 同步一下", ["Ada"]],
  ["@allison @Ada 同步一下", ["Ada"]],
])("完整的 @all 才通知全体：%s", async (text, mention) => {
  api.group = group({});
  renderPage();
  fireEvent.change(screen.getByLabelText("群消息"), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "发送到群" }));
  await waitFor(() => expect(api.post).toHaveBeenCalledWith({ connectionId: "connection", groupId: "group-1", deliveryId: expect.any(String), text, mention }));
});

it("切换对话分别保留群草稿和引用，发送后清除", async () => {
  api.group = group({ messages: [{ message_id: "source", sender: "Ada", text: "长报告", mentioned_agents: [], reply_to: null, occurred_at: "2026-10-18T10:00:00Z" }] });
  const first = renderPage();
  fireEvent.contextMenu(screen.getByRole("article", { name: "Ada的群消息" }).querySelector(".group-message-bubble")!, { clientX: 100, clientY: 100 });
  fireEvent.click(await screen.findByRole("menuitem", { name: "引用 Ada 的消息" }));
  await screen.findByRole("button", { name: "取消引用" });
  fireEvent.change(screen.getByLabelText("群消息"), { target: { value: "尚未发送的判断" } });
  first.unmount();
  api.group = group({ group_id: "group-2", name: "另一个群" });
  const second = renderPage("group-2");
  expect(screen.getByLabelText("群消息")).toHaveValue("");
  second.unmount();
  api.group = group({});
  renderPage();
  expect(screen.getByLabelText("群消息")).toHaveValue("尚未发送的判断");
  expect(screen.getByRole("button", { name: "取消引用" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "发送到群" }));
  await waitFor(() => expect(screen.getByLabelText("群消息")).toHaveValue(""));
  expect(localStorage.getItem("redpanda.composerDraft.group:group-1")).toBeNull();
});

it("正文点名与头像同色，代码和邮箱保持原文", () => {
  api.group = group({ messages: [{ message_id: "source", sender: "Lin", text: "请 @Ada 阅读，邮箱 dev@Ada。\n\n`@Ada`\n\n```txt\n@Ada\n```", mentioned_agents: ["Ada"], reply_to: null, occurred_at: "2026-10-18T10:00:00Z" }] });
  const { container } = renderPage();
  expect(container.querySelectorAll(".message-mention")).toHaveLength(1);
  expect(container.querySelector(".message-mention")).toHaveTextContent("@Ada");
  expect(container.querySelector("code")).toHaveTextContent("@Ada");
});

it("发送请求未结束时离开群聊，成功后不恢复已发送草稿", async () => {
  let complete!: () => void;
  const sent = new Promise<void>((resolve) => { complete = resolve; });
  api.post.mockReturnValueOnce({ unwrap: () => sent });
  api.group = group({});
  const page = renderPage();
  fireEvent.change(screen.getByLabelText("群消息"), { target: { value: "发送后马上去私聊" } });
  fireEvent.click(screen.getByRole("button", { name: "发送到群" }));
  page.unmount();
  complete();
  await waitFor(() => expect(localStorage.getItem("redpanda.composerDraft.group:group-1")).toBeNull());
  renderPage();
  expect(screen.getByLabelText("群消息")).toHaveValue("");
});


it("发送确认丢失后重新打开群页，重试沿用原消息身份", async () => {
  api.group = group({});
  api.post.mockImplementationOnce(() => ({ unwrap: () => Promise.reject("连接中断") }));
  const page = renderPage();
  fireEvent.change(screen.getByLabelText("群消息"), { target: { value: "同一条消息" } });
  fireEvent.click(screen.getByRole("button", { name: "发送到群" }));
  await screen.findByText("连接中断");
  const original = api.post.mock.calls[0][0].deliveryId;
  page.unmount();
  renderPage();
  expect(screen.getByLabelText("群消息")).toHaveValue("同一条消息");
  fireEvent.click(screen.getByRole("button", { name: "发送到群" }));
  await waitFor(() => expect(screen.getByLabelText("群消息")).toHaveValue(""));
  expect(api.post.mock.calls[1][0].deliveryId).toBe(original);
  fireEvent.change(screen.getByLabelText("群消息"), { target: { value: "同一条消息" } });
  fireEvent.click(screen.getByRole("button", { name: "发送到群" }));
  await waitFor(() => expect(api.post).toHaveBeenCalledTimes(3));
  expect(api.post.mock.calls[2][0].deliveryId).not.toBe(original);
});
