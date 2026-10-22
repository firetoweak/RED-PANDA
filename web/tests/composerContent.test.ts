import { expect, it } from "vitest";

import { composeSendContent } from "../src/features/conversation/composerContent";

it("发送内容按各自类型编号附件，并保留正文", () => {
  expect(composeSendContent("", [{ kind: "file" }, { kind: "image" }, { kind: "file" }])).toBe("[File #1] [Image #1] [File #2]");
  expect(composeSendContent("下一句", [{ kind: "image" }, { kind: "image" }])).toBe("下一句 [Image #1] [Image #2]");
});
