import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";

import { BotAvatar, BotAvatars, BotIdentities, useIdentityColor } from "../src/features/conversation/ChatPresentation";

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

it("自选线条同步头像和署名身份色，上传图片沿用 Bot 身份色", () => {
  function Name() {
    const color = useIdentityColor();
    return <strong data-testid="name" data-identity={color("Ada")}>Ada</strong>;
  }
  const view = (avatar: { kind: "generated"; seed: string } | { kind: "image"; attachment_id: string }) => (
    <BotIdentities value={new Map([["Ada", "bot-id"]])}><BotAvatars value={{ "bot-id": avatar }}>
      <div data-testid="chosen"><BotAvatar name="Ada" /></div>
      <div data-testid="preview"><BotAvatar name="Ada" sessionId="bot-id" avatar={avatar} /></div>
      <Name />
    </BotAvatars></BotIdentities>
  );
  const { rerender } = render(view({ kind: "generated", seed: "chosen-seed" }));
  expect(appearance("chosen")).toEqual(appearance("preview"));
  expect(screen.getByTestId("name")).toHaveAttribute("data-identity", appearance("chosen").identity);
  rerender(view({ kind: "image", attachment_id: `sha256:${"a".repeat(64)}` }));
  expect(screen.getByTestId("chosen").querySelector("img")).toHaveAttribute("src", `/api/avatars/images/sha256%3A${"a".repeat(64)}`);
  expect(screen.getByTestId("chosen").querySelector("path")).toBeNull();
  expect(screen.getByTestId("name")).toHaveAttribute("data-identity", screen.getByTestId("chosen").firstElementChild?.getAttribute("data-identity"));
});
