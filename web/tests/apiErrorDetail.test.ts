import { describe, expect, it } from "vitest";

import { apiErrorDetail } from "../src/api/kondenApi";

describe("apiErrorDetail", () => {
  it("keeps a string reason from a known failure", () => {
    expect(apiErrorDetail({ detail: "项目内已有 Agent：konden" })).toBe("项目内已有 Agent：konden");
  });

  it("reads the validation message when the name has surrounding spaces", () => {
    expect(apiErrorDetail({
      detail: [{
        type: "value_error",
        loc: ["body", "name"],
        msg: "Value error, Agent 名字不能以空白开头或结尾",
        input: " konden",
      }],
    })).toBe("Agent 名字不能以空白开头或结尾");
  });
});
