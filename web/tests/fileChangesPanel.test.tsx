import { MantineProvider } from "@mantine/core";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const hooks = vi.hoisted(() => ({ summary: vi.fn(), diff: vi.fn() }));
vi.mock("../src/api/redpandaApi", () => ({ useGetFileChangesQuery: hooks.summary, useGetFileDiffQuery: hooks.diff }));
import { FileChangesPanel } from "../src/features/conversation/FileChangesPanel";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
});

it("retries a failed diff as well as its summary when the file revision stays the same", async () => {
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  const summaryRefetch = vi.fn(), diffRefetch = vi.fn();
  hooks.summary.mockReturnValue({ currentData: {
    files: [{ path: "src/app.py", status: "modified", added: 1, removed: 1, partial: false, binary: false, origin: "agent", revision: "a".repeat(64) }],
    added: 1, removed: 1, partial: false,
  }, isFetching: false, isError: false, refetch: summaryRefetch });
  hooks.diff.mockReturnValue({ currentData: undefined, isFetching: false, isError: true, refetch: diffRefetch });
  render(<MantineProvider><FileChangesPanel sessionId="session" /></MantineProvider>);
  fireEvent.click(screen.getByRole("button", { name: "刷新列表" }));
  expect(summaryRefetch).toHaveBeenCalledOnce();
  expect(diffRefetch).toHaveBeenCalledOnce();
});

it("opens a file with line numbers and resets pagination when its revision changes", async () => {
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  let revision = "a".repeat(64);
  const refetch = vi.fn();
  hooks.summary.mockImplementation(() => ({ currentData: {
    files: ["src/app.py", "tests/app.test.py"].map((path) => ({ path, status: "modified", added: 1, removed: 1, partial: false, binary: false, origin: "agent", revision })),
    added: 1, removed: 1, partial: false,
  }, isFetching: false, isError: false, refetch }));
  hooks.diff.mockImplementation(({ offset }) => ({ currentData: {
    partial: false, binary: false, origin: "agent", properties: [], next_offset: offset === 0 ? 200 : null,
    lines: [{ kind: "delete", old_line: 12, new_line: null, origin: "agent", text: "old value\n" },
      { kind: "add", old_line: null, new_line: 12, origin: "agent", text: "new value\n" }],
  }, isFetching: false, isError: false }));
  const view = render(<MantineProvider><FileChangesPanel sessionId="session" /></MantineProvider>);
  expect(screen.getByLabelText("src/app.py 的差异")).toHaveTextContent("12");
  expect(screen.getByText("old value")).toBeInTheDocument();
  expect(screen.getByText("new value")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "下一页" }));
  expect(screen.getByText("第 2 页")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "查看 tests/app.test.py 的差异" }));
  expect(screen.getByText("第 1 页")).toBeInTheDocument();
  expect(screen.getByLabelText("tests/app.test.py 的差异")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "文件列表" }));
  expect(screen.queryByLabelText("改动文件列表")).not.toBeInTheDocument();
  expect(screen.getByLabelText("tests/app.test.py 的差异")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "下一页" }));
  revision = "b".repeat(64);
  view.rerender(<MantineProvider><FileChangesPanel sessionId="session" /></MantineProvider>);
  expect(screen.getByText("第 1 页")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "刷新文件变更" }));
  expect(refetch).toHaveBeenCalledOnce();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});
