import { MantineProvider } from "@mantine/core";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

import { StepDisclosure } from "../src/features/conversation/ExecutionProcess";
import { timelineTurns } from "../src/features/conversation/timelineTurns";
import type { VisibleStep } from "../src/features/conversation/visibleTimeline";
import { FileAttachmentTile } from "../src/features/conversation/FileAttachmentTile";

const file = {
  attachment_id: `file:${"a".repeat(32)}`,
  name: "evidence.txt",
  size: 6,
};

function step(key: string, text: string, files: VisibleStep["files"] = []): VisibleStep {
  return {
    key, kind: "step", outputId: key, occurredAt: "2026-10-19T00:00:00+00:00",
    stepId: key, text, thinking: null, thinkingPending: false, pending: false,
    tools: [], files,
  };
}

it("带附件的消息单独成气泡，子 Agent 执行步骤里也能下载", async () => {
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  const turns = timelineTurns([
    { ...step("send", ""), text: null, tools: [{
      commandId: "send", name: "send_file", status: "succeeded", error: null,
      arguments: { text: "证据在这", path: "evidence.txt" },
    }] },
    step("file", "证据在这", [file]),
    step("close", "说完了"),
  ]);
  expect(turns.map((turn) => turn.final?.text)).toEqual(["证据在这", "说完了"]);
  expect(turns[0].final?.files).toEqual([file]);
  expect(turns[0].process).toHaveLength(1);

  render(
    <MantineProvider>
      <StepDisclosure
        authorizationDisabled={false}
        onAuthorize={() => {}}
        sessionId="ada"
        index={0}
        step={step("file", "证据在这", [file])}
      />
    </MantineProvider>,
  );
  fireEvent.click(await screen.findByRole("button", { name: /证据在这/ }));
  expect(screen.getByRole("link", { name: "evidence.txt", hidden: true }).getAttribute("href"))
    .toContain(encodeURIComponent(file.attachment_id));
  expect(screen.queryByRole("button", { name: "evidence.txt", hidden: true })).not.toBeInTheDocument();
});

it("交付的图片文件显示缩略图，点击放大，保留原文件下载", () => {
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  const href = `/api/sessions/ada/attachments/${encodeURIComponent(file.attachment_id)}`;
  render(<MantineProvider env="test">
    <FileAttachmentTile name="e2e-03-stopped.PNG" size={83000} href={href} />
  </MantineProvider>);
  const preview = screen.getByRole("button", { name: "e2e-03-stopped.PNG" });
  expect(preview.querySelector("img")).toHaveAttribute("src", href);
  expect(screen.getByRole("link", { name: "e2e-03-stopped.PNG" })).toHaveAttribute("download");
  fireEvent.click(preview);
  expect(within(screen.getByRole("dialog", { name: "e2e-03-stopped.PNG" }))
    .getByRole("img", { name: "e2e-03-stopped.PNG" })).toHaveAttribute("src", href);
});
