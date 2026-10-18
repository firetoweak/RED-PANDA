import { describe, expect, it } from "vitest";

import { apiErrorDetail } from "../src/api/redpandaApi";

describe("apiErrorDetail", () => {
  it("keeps a string reason from a known failure", () => {
    expect(apiErrorDetail({ detail: "项目内已有 Agent：小熊猫" })).toBe("项目内已有 Agent：小熊猫");
  });

  it("reads the validation message when the name has surrounding spaces", () => {
    expect(apiErrorDetail({
      detail: [{
        type: "value_error",
        loc: ["body", "name"],
        msg: "Value error, Agent 名字不能以空白开头或结尾",
        input: " 小熊猫",
      }],
    })).toBe("Agent 名字不能以空白开头或结尾");
  });
});
