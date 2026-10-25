import { MantineProvider } from "@mantine/core";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useState } from "react";

import { ErrorDetails, ErrorNotice } from "../src/features/conversation/ErrorNotice";
import { ContentPage } from "../src/features/conversation/ContentPage";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("长错误默认收起，内容页提供全文和复制，打开时仍可操作历史", async () => {
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  const writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText }, userAgent: navigator.userAgent });
  const message = `ServiceFailure: Filesystem metadata is corrupt\nTraceback (most recent call last):\n${"    File worker.py, line 246\n".repeat(100)}origin points to missing delta inode`;
  const operateHistory = vi.fn();
  function Harness() {
    const [opened, setOpened] = useState(false);
    return <div>
      <button onClick={operateHistory}>历史操作</button>
      <ErrorNotice title="执行已停止" message={message} onOpen={() => setOpened(true)} />
      {!opened ? null : <ContentPage title="错误详情" onClose={() => setOpened(false)}>
        <ErrorDetails message={message} />
      </ContentPage>}
    </div>;
  }
  render(<MantineProvider><Harness /></MantineProvider>);

  expect(screen.getByRole("alert")).toHaveTextContent("执行已停止。ServiceFailure: Filesystem metadata is corrupt");
  expect(screen.getByRole("alert")).not.toHaveTextContent("Traceback");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "查看详情" }));
  const details = screen.getByRole("region", { name: "错误详情" });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(details.querySelector("pre")?.textContent).toBe(message);
  fireEvent.click(screen.getByRole("button", { name: "复制完整错误" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith(message));
  fireEvent.click(screen.getByRole("button", { name: "历史操作" }));
  expect(operateHistory).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole("button", { name: "关闭内容页" }));
  expect(screen.queryByRole("region", { name: "错误详情" })).not.toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent("执行已停止");
});
