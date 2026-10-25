import { describe, expect, it } from "vitest";

import {
  isSubagentTool,
  turnNeedsSubagentHint,
} from "../src/features/conversation/subagent";

describe("subagent call identity", () => {
  it("treats only delegate and reclaim as SubAgent tools", () => {
    expect(isSubagentTool("delegate")).toBe(true);
    expect(isSubagentTool("reclaim")).toBe(true);
    expect(isSubagentTool("execute_command")).toBe(false);
  });
});

describe("subagent activity hint", () => {
  it("shows only on the latest turn while children are still pending", () => {
    expect(turnNeedsSubagentHint(true, true)).toBe(true);
    expect(turnNeedsSubagentHint(false, true)).toBe(false);
    expect(turnNeedsSubagentHint(true, false)).toBe(false);
  });
});
