import { beforeEach, describe, expect, it } from "vitest";

import {
  readCollapsedWorkspaces,
  writeCollapsedWorkspaces,
} from "../src/features/sessions/workspaces";

describe("折叠偏好", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("只记住被折叠的工作区", () => {
    writeCollapsedWorkspaces({ a: true, b: false });

    expect(readCollapsedWorkspaces()).toEqual({ a: true });
  });

  it("没有写入过时返回空对象", () => {
    expect(readCollapsedWorkspaces()).toEqual({});
  });

  it("存的内容不是对象时返回空对象", () => {
    window.localStorage.setItem("redpanda.collapsedWorkspaces", "[1,2]");

    expect(readCollapsedWorkspaces()).toEqual({});
  });
});
