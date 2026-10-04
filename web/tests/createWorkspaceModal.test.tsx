import { MantineProvider } from "@mantine/core";
import { configureStore } from "@reduxjs/toolkit";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Provider } from "react-redux";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { redpandaApi } from "../src/api/redpandaApi";
import { CreateWorkspaceModal } from "../src/features/sessions/CreateWorkspaceModal";

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  vi.stubGlobal("matchMedia", () => ({
    matches: false, addEventListener() {}, removeEventListener() {},
  }));
  const NativeRequest = Request;
  vi.stubGlobal("Request", class extends NativeRequest {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      super(typeof input === "string" ? new URL(input, "http://localhost") : input, init);
    }
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function showModal() {
  const store = configureStore({
    reducer: { [redpandaApi.reducerPath]: redpandaApi.reducer },
    middleware: (getDefaultMiddleware) => getDefaultMiddleware().concat(redpandaApi.middleware),
  });
  const onClose = vi.fn();
  render(
    <Provider store={store}><MantineProvider>
      <CreateWorkspaceModal opened onClose={onClose} />
    </MantineProvider></Provider>,
  );
  return { store, onClose };
}

it("选择目录后回填名称和路径，显式创建时才提交工作区", async () => {
  const submissions: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => {
    expect(request.method).toBe("POST");
    if (new URL(request.url).pathname === "/api/workspaces/select-directory") {
      return Response.json({ directory: { path: "D:\\项目\\助手", name: "助手" } });
    }
    expect(new URL(request.url).pathname).toBe("/api/workspaces");
    submissions.push(await request.json());
    return Response.json({
      workspace_id: "workspace-test", name: "助手", task_root: "D:\\项目\\助手",
      full_access: false, created_at: "2026-09-27T00:00:00Z",
    }, { status: 201 });
  }));
  const { store, onClose } = showModal();
  fireEvent.click(screen.getByRole("button", { name: "选择文件夹" }));
  await waitFor(() => expect(screen.getByLabelText("目录")).toHaveValue("D:\\项目\\助手"));
  expect(screen.getByLabelText("名称")).toHaveValue("助手");
  expect(submissions).toEqual([]);
  fireEvent.click(screen.getByRole("button", { name: "创建" }));
  await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
  expect(submissions).toEqual([{ name: "助手", task_root: "D:\\项目\\助手", full_access: false }]);
  store.dispatch(redpandaApi.util.resetApiState());
});

it.each([
  [null, "/old/path"],
  [{ path: "/home/me/project", name: "project" }, "/home/me/project"],
])("取消不改变表单，重新选择不覆盖自定义名称：%j", async (directory, expectedPath) => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ directory })));
  const { store, onClose } = showModal();
  fireEvent.change(screen.getByLabelText("名称"), { target: { value: "我的工作区" } });
  fireEvent.change(screen.getByLabelText("目录"), { target: { value: "/old/path" } });
  fireEvent.click(screen.getByRole("button", { name: "选择文件夹" }));
  await waitFor(() => expect(screen.getByLabelText("目录")).toBeEnabled());
  expect(screen.getByLabelText("目录")).toHaveValue(expectedPath);
  expect(screen.getByLabelText("名称")).toHaveValue("我的工作区");
  expect(onClose).not.toHaveBeenCalled();
  expect(screen.queryByText("选择失败")).toBeNull();
  store.dispatch(redpandaApi.util.resetApiState());
});

it("图形环境不可用时显示后端原因，并保留手填目录的入口", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json(
    { detail: "当前 Python 未安装 Tcl/Tk，无法打开文件夹选择窗口。" },
    { status: 503 },
  )));
  const { store } = showModal();
  fireEvent.click(screen.getByRole("button", { name: "选择文件夹" }));
  await screen.findByText("当前 Python 未安装 Tcl/Tk，无法打开文件夹选择窗口。");
  expect(screen.getByLabelText("目录")).toBeEnabled();
  expect(screen.getByLabelText("目录")).toHaveValue("");
  store.dispatch(redpandaApi.util.resetApiState());
});
