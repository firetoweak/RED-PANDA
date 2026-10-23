import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";

import { BotAvatar, BotIdentities } from "../src/features/conversation/ChatPresentation";

afterEach(cleanup);

function appearance(testId: string) {
  const root = screen.getByTestId(testId);
  const path = root.querySelector("path")?.getAttribute("d");
  const identity = root.querySelector(".agent-avatar")?.getAttribute("data-identity");
  expect(path).toBeTruthy();
  expect(identity).toBeTruthy();
  return { path, identity };
}

it("同一 Bot 的显式 ID、名字解析和改名入口保留相同头像与底色", () => {
  const sessionId = "agent-avatar-stable";
  const view = (name: string) => (
    <BotIdentities.Provider value={new Map([[name, sessionId]])}>
      <div data-testid="explicit"><BotAvatar name={name} sessionId={sessionId} /></div>
      <div data-testid="resolved"><BotAvatar name={name} /></div>
    </BotIdentities.Provider>
  );
  const { rerender } = render(view("Atlas"));
  const original = appearance("explicit");
  expect(appearance("resolved")).toEqual(original);

  rerender(view("Aria"));
  expect(appearance("explicit")).toEqual(original);
  expect(appearance("resolved")).toEqual(original);
});
