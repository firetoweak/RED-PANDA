import { MantineProvider } from "@mantine/core";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { WorkspaceFileResultAlert } from "../src/features/conversation/WorkspaceFileResultAlert";
import type { ConversationView } from "../src/api/contracts";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("keeps session success distinct from file failure and exposes the failed path", () => {
  vi.stubGlobal("matchMedia", () => ({
    matches: false, addEventListener() {}, removeEventListener() {},
  }));
  render(<MantineProvider><WorkspaceFileResultAlert result={{
    policy: "preserve", effect: "failed", changed_files: [], preserved_files: [],
    deleted_files: [], failed_files: ["calc.py"], error: "文件长度已变化，无法安全恢复。",
  }} /></MantineProvider>);
  expect(screen.getByRole("status")).toHaveTextContent("会话已回退");
  expect(screen.getByRole("status")).toHaveTextContent("文件未能恢复");
  expect(screen.getByText("calc.py")).toBeInTheDocument();
  expect(screen.getByText("文件长度已变化，无法安全恢复。")).toBeInTheDocument();
});

it("exposes every changed path, actual deletion and the original-policy warning", () => {
  vi.stubGlobal("matchMedia", () => ({
    matches: false, addEventListener() {}, removeEventListener() {},
  }));
  render(<MantineProvider><WorkspaceFileResultAlert result={{
    policy: "original", effect: "restored",
    changed_files: Array.from({ length: 105 }, (_, index) => `file-${index}.py`),
    preserved_files: [], deleted_files: ["file-0.py"], failed_files: [], error: null,
  }} /></MantineProvider>);
  expect(screen.getByText("file-104.py")).toBeInTheDocument();
  expect(screen.getByText("其中已删除（1）")).toBeInTheDocument();
  expect(screen.getByText(/原值策略允许覆盖/)).toBeInTheDocument();
});

const unchangedResult: NonNullable<ConversationView["workspace_file_result"]> = {
  policy: "preserve", effect: "unchanged", changed_files: [], preserved_files: [],
  deleted_files: [], failed_files: [], error: null,
};

it("手动关闭文件处理提示后，查询刷新不再显示", () => {
  vi.stubGlobal("matchMedia", () => ({
    matches: false, addEventListener() {}, removeEventListener() {},
  }));
  const { rerender } = render(<MantineProvider>
    <WorkspaceFileResultAlert result={unchangedResult} />
  </MantineProvider>);
  fireEvent.click(screen.getByRole("button", { name: "关闭文件处理提示" }));
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  rerender(<MantineProvider>
    <WorkspaceFileResultAlert result={{ ...unchangedResult }} />
  </MantineProvider>);
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});

it("继续执行后自动收起文件处理提示，执行结束也不再显示", () => {
  vi.stubGlobal("matchMedia", () => ({
    matches: false, addEventListener() {}, removeEventListener() {},
  }));
  const { rerender } = render(<MantineProvider>
    <WorkspaceFileResultAlert result={unchangedResult} running={false} />
  </MantineProvider>);
  expect(screen.getByRole("status")).toBeInTheDocument();
  rerender(<MantineProvider>
    <WorkspaceFileResultAlert result={unchangedResult} running />
  </MantineProvider>);
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  rerender(<MantineProvider>
    <WorkspaceFileResultAlert result={{ ...unchangedResult }} running={false} />
  </MantineProvider>);
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});
