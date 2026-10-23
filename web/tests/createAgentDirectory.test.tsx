import { MantineProvider } from "@mantine/core";
import { configureStore } from "@reduxjs/toolkit";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Provider } from "react-redux";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { redpandaApi } from "../src/api/redpandaApi";
import type { Workspace } from "../src/api/contracts";
import { CreateAgentModal } from "../src/features/sessions/CreateAgentModal";
import { LinkProjectModal } from "../src/features/sessions/LinkProjectModal";

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  Element.prototype.scrollIntoView = vi.fn();
  const NativeRequest = Request;
  vi.stubGlobal("Request", class extends NativeRequest {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      super(typeof input === "string" ? new URL(input, "http://localhost") : input, init);
    }
  });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const agent = { session_id: "agent-test", name: "Ada", projects: [], activity: "idle", status: "idle" } as const;
const projects: Workspace[] = ["C:/one/app", "C:/two/app"].map((path, i) => ({
  workspace_id: `workspace-${i}`, name: "app", task_root: path, full_access: false, created_at: "2026-10-23",
}));

function showModal(workspaces: Workspace[] = [], link = false) {
  const store = configureStore({
    reducer: { [redpandaApi.reducerPath]: redpandaApi.reducer },
    middleware: (defaults) => defaults().concat(redpandaApi.middleware),
  });
  const onClose = vi.fn();
  const onCreated = vi.fn();
  render(<Provider store={store}><MantineProvider env="test">
    {link ? <LinkProjectModal opened agent={{ ...agent, projects: [] }} workspaces={workspaces} connectionId="connection" onClose={onClose} />
      : <CreateAgentModal opened workspaces={workspaces} connectionId="connection" onClose={onClose} onCreated={onCreated} />}
  </MantineProvider></Provider>);
  return { store, onClose, onCreated };
}

it("选目录只填写同页草稿，取消创建不登记项目", async () => {
  const fetch = vi.fn(async (request: Request) => {
    expect(new URL(request.url).pathname).toBe("/api/workspaces/select-directory");
    return Response.json({ directory: { path: "D:/项目/助手", name: "助手" } });
  });
  vi.stubGlobal("fetch", fetch);
  const { store, onClose } = showModal();
  fireEvent.change(screen.getByLabelText("名字"), { target: { value: "Ada" } });
  fireEvent.click(screen.getByRole("button", { name: "选择文件夹" }));
  await waitFor(() => expect(screen.getByRole("combobox", { name: "代码目录（可选）", exact: true })).toHaveValue("D:/项目/助手"));
  expect(screen.getByLabelText("名字")).toHaveValue("Ada");
  expect(screen.getAllByRole("dialog")).toHaveLength(1);
  fireEvent.keyDown(screen.getByLabelText("名字"), { key: "Escape" });
  await waitFor(() => expect(onClose).toHaveBeenCalled());
  expect(fetch).toHaveBeenCalledOnce();
  store.dispatch(redpandaApi.util.resetApiState());
});

it.each(["", " D:/项目/助手 "])("确认时一次提交名字和可选目录：%j", async (path) => {
  const submissions: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => {
    expect(new URL(request.url).pathname).toBe("/api/agents");
    submissions.push(await request.json());
    return Response.json(agent, { status: 201 });
  }));
  const { store, onCreated } = showModal();
  fireEvent.change(screen.getByLabelText("名字"), { target: { value: "Ada" } });
  fireEvent.change(screen.getByRole("combobox", { name: "代码目录（可选）", exact: true }), { target: { value: path } });
  fireEvent.click(screen.getByRole("button", { name: "创建", exact: true }));
  await waitFor(() => expect(onCreated).toHaveBeenCalledWith("agent-test"));
  expect(submissions).toEqual([{ connection_id: "connection", path: path.trim() || null, name: "Ada" }]);
  store.dispatch(redpandaApi.util.resetApiState());
});

it("同名项目同时展示完整路径，后续关联使用选择的目录", async () => {
  const submissions: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => {
    expect(new URL(request.url).pathname).toBe("/api/agents/agent-test/projects");
    submissions.push(await request.json());
    return Response.json(agent);
  }));
  const { store, onClose } = showModal(projects, true);
  fireEvent.click(screen.getByRole("combobox", { name: "代码目录", exact: true }));
  expect(screen.getByText("C:/one/app")).toBeVisible();
  fireEvent.click(screen.getByText("C:/two/app"));
  expect(screen.getByRole("combobox", { name: "代码目录", exact: true })).toHaveValue("C:/two/app");
  fireEvent.click(screen.getByRole("button", { name: "关联", exact: true }));
  await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
  expect(submissions).toEqual([{ connection_id: "connection", workspace_id: null, path: "C:/two/app" }]);
  store.dispatch(redpandaApi.util.resetApiState());
});

it("选择取消保留原目录，桌面不可用时可继续手填", async () => {
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(Response.json({ directory: null }))
    .mockResolvedValueOnce(Response.json({ detail: "当前 Python 未安装 Tcl/Tk" }, { status: 503 })));
  const { store } = showModal();
  fireEvent.change(screen.getByRole("combobox", { name: "代码目录（可选）", exact: true }), { target: { value: "C:/existing" } });
  fireEvent.click(screen.getByRole("button", { name: "选择文件夹" }));
  await waitFor(() => expect(screen.getByRole("combobox", { name: "代码目录（可选）", exact: true })).toBeEnabled());
  expect(screen.getByRole("combobox", { name: "代码目录（可选）", exact: true })).toHaveValue("C:/existing");
  fireEvent.click(screen.getByRole("button", { name: "选择文件夹" }));
  await screen.findByText("当前 Python 未安装 Tcl/Tk");
  expect(screen.getByRole("combobox", { name: "代码目录（可选）", exact: true })).toBeEnabled();
  expect(screen.getByRole("combobox", { name: "代码目录（可选）", exact: true })).toHaveValue("C:/existing");
  store.dispatch(redpandaApi.util.resetApiState());
});
