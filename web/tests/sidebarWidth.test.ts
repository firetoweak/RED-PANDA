import { beforeEach, describe, expect, it } from "vitest";

import {
  DEFAULT_SIDEBAR_WIDTH,
  MAX_SIDEBAR_WIDTH,
  MIN_SIDEBAR_WIDTH,
  clampSidebarWidth,
  readSidebarWidth,
  writeSidebarWidth,
} from "../src/app/sidebarWidth";

describe("sidebarWidth", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("把宽度夹在可拖区间里", () => {
    expect(clampSidebarWidth(80)).toBe(MIN_SIDEBAR_WIDTH);
    expect(clampSidebarWidth(900)).toBe(MAX_SIDEBAR_WIDTH);
    expect(clampSidebarWidth(320)).toBe(320);
  });

  it("记住合法宽度，缺失或非法值用默认宽度", () => {
    expect(readSidebarWidth()).toBe(DEFAULT_SIDEBAR_WIDTH);
    writeSidebarWidth(360);
    expect(readSidebarWidth()).toBe(360);

    window.localStorage.setItem("konden.sidebarWidth", "不是数字");
    expect(readSidebarWidth()).toBe(DEFAULT_SIDEBAR_WIDTH);
  });
});
