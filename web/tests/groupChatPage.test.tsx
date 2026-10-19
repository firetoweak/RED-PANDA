import { MantineProvider } from "@mantine/core";
import { configureStore } from "@reduxjs/toolkit";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  useGetAgentsQuery: () => ({ data: [{
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
  // Ada's member badge and her message both lead to her session, where a person can stop her.
  const links = screen.getAllByRole("link", { name: "Ada" });
  expect(links).toHaveLength(2);
  links.forEach((link) => expect(link).toHaveAttribute("href", "/sessions/agent-ada"));
  expect(screen.getByText("@Lin")).toBeInTheDocument();
  expect(screen.getByText("正在等 Lin 回应…")).toBeInTheDocument();
});

it("发言带上点名", async () => {
  api.group = group({});
  renderPage();
  fireEvent.click(screen.getByRole("combobox", { name: "点名" }));
  fireEvent.click(await screen.findByRole("option", { name: "Lin" }));
  fireEvent.change(screen.getByLabelText("群消息"), { target: { value: "Lin 看看测试" } });
  fireEvent.click(screen.getByLabelText("发送到群"));
  await waitFor(() => expect(api.post).toHaveBeenCalledWith({
    connectionId: "connection", groupId: "group-1", text: "Lin 看看测试", mention: ["Lin"],
  }));
});
