import { MantineProvider } from "@mantine/core";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

import { StepDisclosure } from "../src/features/conversation/ExecutionProcess";
import { timelineTurns } from "../src/features/conversation/timelineTurns";
import type { VisibleStep } from "../src/features/conversation/visibleTimeline";
import { FileAttachmentTile } from "../src/features/conversation/FileAttachmentTile";
import { conversationViewSchema } from "../src/api/contracts";

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

it("图片只显示画面，文件名和大小不固定占位，点开放大后可下载原图", () => {
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  const href = `/api/sessions/ada/attachments/${encodeURIComponent(file.attachment_id)}`;
  render(<MantineProvider env="test">
    <FileAttachmentTile name="e2e-03-stopped.PNG" size={83000} href={href} />
  </MantineProvider>);
  const preview = screen.getByRole("button", { name: "e2e-03-stopped.PNG" });
  expect(preview.querySelector("img")).toHaveAttribute("src", href);
  expect(screen.queryByRole("link")).not.toBeInTheDocument();
  expect(screen.queryByText("e2e-03-stopped.PNG")).not.toBeInTheDocument();
  expect(screen.queryByText(/KB/)).not.toBeInTheDocument();
  fireEvent.click(preview);
  expect(within(screen.getByRole("dialog", { name: "e2e-03-stopped.PNG" }))
    .getByRole("img", { name: "e2e-03-stopped.PNG" })).toHaveAttribute("src", href);
  expect(within(screen.getByRole("dialog", { name:/e2e-03-stopped.PNG/ }))
    .getByRole("link", {name:"下载原图"})).toHaveAttribute("href", href);
});

it("无说明的图片交付仍是完整消息，接口和时间线不会丢失附件", () => {
  const picture = {...file,name:"picture.png"};
  const imageStep = {...step("picture", "", [picture]),text:null};
  const schema = conversationViewSchema.shape.items.element;
  expect(schema.parse({kind:"step",step_id:"picture",output_id:"picture",text:null,
    thinking:null,tools:[],files:[picture],occurred_at:"2026-10-19T00:00:00+00:00"})).toMatchObject({text:null,files:[picture]});
  const turns = timelineTurns([imageStep,step("analysis","蓝色背景突出橙色主体。")]);
  expect(turns[0].final?.files).toEqual([picture]);
  expect(turns[0].final?.text).toBeNull();
  expect(turns[1].final?.text).toBe("蓝色背景突出橙色主体。");
});
