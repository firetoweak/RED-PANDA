import { afterEach, expect, it, vi } from "vitest";

import { createClientId } from "../src/features/conversation/clientId";

afterEach(() => vi.unstubAllGlobals());

it("没有 randomUUID 的普通 HTTP 环境也能生成不同的客户端标识", () => {
  vi.stubGlobal("crypto", {
    getRandomValues: globalThis.crypto.getRandomValues.bind(globalThis.crypto),
  });

  const first = createClientId();
  const second = createClientId();

  expect(first).toMatch(/^[0-9a-f]{32}$/);
  expect(second).toMatch(/^[0-9a-f]{32}$/);
  expect(second).not.toBe(first);
});
