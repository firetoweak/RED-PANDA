import { describe, expect, it } from "vitest";

import { stepHeading } from "../src/features/conversation/stepHeading";
import type {
  VisibleStep,
  VisibleTool,
} from "../src/features/conversation/visibleTimeline";

type HeadingStep = Pick<VisibleStep, "text" | "tools" | "thinkingPending">;

function step(overrides: Partial<HeadingStep> = {}): HeadingStep {
  return { text: null, tools: [], thinkingPending: false, ...overrides };
}

function tool(
  name: string,
  arguments_: Record<string, unknown> = {},
): VisibleTool {
  return {
    commandId: name,
    name,
    status: "succeeded",
    error: null,
    arguments: arguments_,
  };
}

describe("stepHeading", () => {
  it("prefers the step intent over the call preview", () => {
    expect(
      stepHeading(
        step({
          text: "先确认工作区结构，再定位会话存储",
          tools: [tool("read_file", { path: "a.py" })],
        }),
      ),
    ).toBe("先确认工作区结构，再定位会话存储");
  });

  it("falls back to the call preview when the step says nothing", () => {
    expect(
      stepHeading(
        step({
          tools: [
            tool("execute_command", {
              command: "Get-ChildItem -Recurse D:\\w",
            }),
          ],
        }),
      ),
    ).toBe("execute_command  Get-ChildItem -Recurse D:\\w");
  });

  it("keeps only the first string argument of a tool", () => {
    expect(
      stepHeading(
        step({ tools: [tool("read_file", { path: "a.py", other: "b.py" })] }),
      ),
    ).toBe("read_file  a.py");
  });

  it("uses the bare tool name when it has no string argument", () => {
    expect(stepHeading(step({ tools: [tool("list_processes", { patch: 3 })] }))).toBe(
      "list_processes",
    );
  });

  it("joins several tool calls in order", () => {
    expect(
      stepHeading(
        step({
          tools: [
            tool("read_file", { path: "a.py" }),
            tool("grep", { pattern: "foo" }),
          ],
        }),
      ),
    ).toBe("read_file  a.py · grep  foo");
  });

  it("clips the tool hint", () => {
    const full = `grep  ${"x".repeat(80)}`;
    const heading = stepHeading(step({ tools: [tool("grep", { pattern: "x".repeat(80) })] }));
    expect(heading.endsWith("…")).toBe(true);
    expect(full.startsWith(heading.slice(0, -1))).toBe(true);
    expect(heading.length).toBeLessThan(full.length);
  });

  it("uses the first non-empty line of the intent and strips list markers", () => {
    expect(stepHeading(step({ text: "\n- 先看目录\n再看别的" }))).toBe(
      "先看目录",
    );
  });

  it("clips a long intent on one line", () => {
    const full = "x".repeat(80);
    const heading = stepHeading(step({ text: full }));
    expect(heading.endsWith("…")).toBe(true);
    expect(full.startsWith(heading.slice(0, -1))).toBe(true);
    expect(heading.length).toBeLessThan(full.length);
  });

  it("shows 思考中 while a step is still only outputting thinking", () => {
    expect(stepHeading(step({ thinkingPending: true }))).toBe("思考中");
  });

  it("falls back to a generic label when there is nothing to say", () => {
    expect(stepHeading(step({ text: "   " }))).toBe("Step");
    expect(stepHeading(step())).toBe("Step");
  });
});
