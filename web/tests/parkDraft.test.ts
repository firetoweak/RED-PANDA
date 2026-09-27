import { describe, expect, it } from "vitest";

import {
  composeSendContent,
  joinRestoredText,
  parkedPreviewText,
  restoreParkedDraft,
} from "../src/features/conversation/parkDraft";

describe("parked composer draft", () => {
  it("returns the parked text to the editor instead of discarding it", () => {
    const restored = restoreParkedDraft(
      {
        text: "预先输入的下一句",
        pending: [
          {
            localId: "img-1",
            name: "shot.png",
            previewUrl: "blob:parked",
            kind: "image", file: new File([], "shot.png"), size: 0, sourcePath: null, error: null,
            attachmentId: "att-1",
            state: "done",
          },
        ],
      },
      { text: "", pending: [] },
    );

    expect(restored.text).toBe("预先输入的下一句");
    expect(restored.pending).toEqual([
      {
        localId: "img-1",
        name: "shot.png",
        previewUrl: "blob:parked",
            kind: "image", file: new File([], "shot.png"), size: 0, sourcePath: null, error: null,
        attachmentId: "att-1",
        state: "done",
      },
    ]);
  });

  it("keeps later editor typing after the restored parked text", () => {
    expect(joinRestoredText("先发这句", "再补一句")).toBe("先发这句\n再补一句");
  });

  it("does not rewrite image tokens until the parked draft actually sends", () => {
    expect(composeSendContent("", [{ kind: "file" }, { kind: "image" }, { kind: "file" }])).toBe("[File #1] [Image #1] [File #2]");
    expect(composeSendContent("下一句", [{ kind: "image" }, { kind: "image" }])).toBe("下一句 [Image #1] [Image #2]");
    expect(parkedPreviewText({ text: "  下一句  ", pending: [] })).toBe("下一句");
    expect(
      parkedPreviewText({
        text: "   ",
        pending: [
          {
            localId: "img-1",
            name: "shot.png",
            previewUrl: "blob:parked",
            kind: "image", file: new File([], "shot.png"), size: 0, sourcePath: null, error: null,
            attachmentId: "att-1",
            state: "done",
          },
        ],
      }),
    ).toBe("附件（1）");
  });
});
