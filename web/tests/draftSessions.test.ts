import { expect, it, vi } from "vitest";

it("新会话的工作区关联跨页面加载保留，首次发送锁定后不再恢复为草稿", async () => {
  localStorage.clear();
  vi.resetModules();
  const { store } = await import("../src/app/store");
  const { setDraftSession, lockDraft } = await import("../src/realtime/runtimeSlice");
  store.dispatch(setDraftSession({ workspaceId: "workspace", sessionId: "draft-session" }));
  sessionStorage.clear();
  vi.resetModules();
  const reloaded = (await import("../src/app/store")).store;
  expect(reloaded.getState().runtime.draftSessions).toEqual({ workspace: "draft-session" });
  reloaded.dispatch(lockDraft("draft-session"));
  vi.resetModules();
  const reopened = (await import("../src/app/store")).store;
  expect(reopened.getState().runtime.draftSessions).toEqual({});
  localStorage.clear();
});
