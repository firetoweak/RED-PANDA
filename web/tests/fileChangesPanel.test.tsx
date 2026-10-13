import { MantineProvider } from "@mantine/core";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

const hooks = vi.hoisted(() => ({ summary: vi.fn(), diff: vi.fn() }));
vi.mock("../src/api/redpandaApi", () => ({ useGetFileChangesQuery: hooks.summary, useGetFileDiffQuery: hooks.diff }));
import { FileChangesPanel } from "../src/features/conversation/FileChangesPanel";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("opens a file with line numbers and resets pagination when its revision changes", async () => {
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  let revision = "a".repeat(64);
  const refetch = vi.fn();
  hooks.summary.mockImplementation(() => ({ currentData: {
    files: [{ path: "src/app.py", status: "modified", added: 1, removed: 1, partial: false, binary: false, origin: "agent", revision }],
    added: 1, removed: 1, partial: false,
  }, isFetching: false, isError: false, refetch }));
  hooks.diff.mockImplementation(({ offset }) => ({ currentData: {
    partial: false, binary: false, origin: "agent", properties: [], next_offset: offset === 0 ? 200 : null,
    lines: [{ kind: "delete", old_line: 12, new_line: null, origin: "agent", text: "old value\n" },
      { kind: "add", old_line: null, new_line: 12, origin: "agent", text: "new value\n" }],
  }, isFetching: false, isError: false }));
  const view = render(<MantineProvider><FileChangesPanel sessionId="session" /></MantineProvider>);
  fireEvent.click(screen.getByRole("button", { name: /1 个文件已更改/ }));
  fireEvent.click(await screen.findByRole("button", { name: /src\/app.py/ }));
  expect(screen.getByLabelText("src/app.py 的差异")).toHaveTextContent("12");
  expect(screen.getByText("old value")).toBeInTheDocument();
  expect(screen.getByText("new value")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "下一页" }));
  expect(screen.getByText("第 2 页")).toBeInTheDocument();
  revision = "b".repeat(64);
  view.rerender(<MantineProvider><FileChangesPanel sessionId="session" /></MantineProvider>);
  expect(screen.getByText("第 1 页")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "刷新文件变更" }));
  expect(refetch).toHaveBeenCalledOnce();
});
