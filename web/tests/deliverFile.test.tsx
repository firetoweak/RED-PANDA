import { MantineProvider } from "@mantine/core";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

import { ExecutionProcess } from "../src/features/conversation/ExecutionProcess";
import { timelineTurns } from "../src/features/conversation/timelineTurns";
import type { VisibleStep } from "../src/features/conversation/visibleTimeline";

const file = {
  attachment_id: `file:${"a".repeat(32)}`,
  name: "evidence.txt",
  size: 6,
};

function step(key: string, text: string, files: VisibleStep["files"] = []): VisibleStep {
  return {
    key, kind: "step", outputId: key, occurredAt: "2026-10-19T00:00:00+00:00",
    stepId: key, text, thinking: null, thinkingPending: false, pending: false,
    tools: [], audience: "user", files,
  };
}

it("带附件的消息单独成气泡，执行过程里展开后也能下载", () => {
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  const turns = timelineTurns([
    step("file", "证据在这", [file]),
    step("close", "说完了"),
  ]);
  expect(turns.map((turn) => turn.final?.text)).toEqual(["证据在这", "说完了"]);
  expect(turns[0].final?.files).toEqual([file]);

  render(
    <MantineProvider>
      <ExecutionProcess
        authorizationDisabled={false}
        complete={false}
        onAuthorize={() => {}}
        sessionId="ada"
        steps={[step("file", "证据在这", [file])]}
      />
    </MantineProvider>,
  );
  fireEvent.click(screen.getByRole("button", { name: /证据在这/ }));
  expect(screen.getByRole("link", { name: "evidence.txt", hidden: true }).getAttribute("href"))
    .toContain(encodeURIComponent(file.attachment_id));
});
