import { MantineProvider } from "@mantine/core";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { WorkspaceFileResultAlert } from "../src/features/conversation/WorkspaceFileResultAlert";
import type { WorkspaceFileResult } from "../src/api/contracts";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderAlert(result: WorkspaceFileResult | null) {
  vi.stubGlobal("matchMedia", () => ({
    matches: false, addEventListener() {}, removeEventListener() {},
  }));
  return render(<MantineProvider><WorkspaceFileResultAlert result={result} /></MantineProvider>);
}

it("文件没能恢复时，提示保留失败路径与原因", () => {
  renderAlert({
    policy: "preserve", effect: "failed", changed_files: [], preserved_files: [],
    deleted_files: [], failed_files: ["calc.py"], error: "文件长度已变化，无法安全恢复。",
  });
  expect(screen.getByRole("status")).toHaveTextContent("会话已回退");
  expect(screen.getByRole("status")).toHaveTextContent("文件未能恢复");
  expect(screen.getByText("calc.py")).toBeInTheDocument();
  expect(screen.getByText("文件长度已变化，无法安全恢复。")).toBeInTheDocument();
});

it("文件部分回退时，提示列出实际改动与原值策略后果", () => {
  renderAlert({
    policy: "original", effect: "partially_restored",
    changed_files: Array.from({ length: 105 }, (_, index) => `file-${index}.py`),
    preserved_files: [], deleted_files: ["file-0.py"], failed_files: [], error: null,
  });
  expect(screen.getByText("file-104.py")).toBeInTheDocument();
  expect(screen.getByText("其中已删除（1）")).toBeInTheDocument();
  expect(screen.getByText(/原值策略允许覆盖/)).toBeInTheDocument();
});

it.each(["unchanged", "restored", "kept"] as const)(
  "文件与回退后的会话一致时（%s）不提示",
  (effect) => {
    renderAlert({
      policy: "preserve", effect, changed_files: [], preserved_files: [],
      deleted_files: [], failed_files: [], error: null,
    });
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  },
);

it("提示不提供关闭按钮，是否还在取决于当前轮次", () => {
  renderAlert({
    policy: "preserve", effect: "preserved", changed_files: [], preserved_files: [],
    deleted_files: [], failed_files: [], error: null,
  });
  expect(screen.getByRole("status")).toBeInTheDocument();
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});

it("没有文件处理结果时不提示", () => {
  renderAlert(null);
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});
