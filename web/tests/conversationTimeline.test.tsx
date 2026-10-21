import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { createRef } from "react";
import { VirtuosoMockContext, type VirtuosoHandle } from "react-virtuoso";
import { beforeEach, afterEach, expect, it, vi } from "vitest";

import { ConversationTimeline } from "../src/features/conversation/ConversationTimeline";
import type { TimelineTurn } from "../src/features/conversation/timelineTurns";

afterEach(() => {
  cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals();
  Reflect.deleteProperty(HTMLElement.prototype, "scrollTo");
});

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(40000);
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(600);
  Object.defineProperty(HTMLElement.prototype, "scrollTo", {
    configurable: true,
    value(this: HTMLElement, options: ScrollToOptions) {
      this.scrollTop = options.top ?? 0;
      this.dispatchEvent(new Event("scroll"));
    },
  });
});

it("打开长会话只挂载末尾附近的轮次，不挂载整段历史", async () => {
  const dockRef = createRef<HTMLDivElement>();
  const turns: TimelineTurn[] = Array.from({ length: 200 }, (_, index) => ({
    key: `turn-${index}`, log: false, user: null, process: [], active: null, final: null,
  }));
  const { container } = render(
    <VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 200 }}>
      <ConversationTimeline
        turns={turns}
        listRef={createRef<VirtuosoHandle>()}
        wheelAreaRef={dockRef}
        following
        onFollowingChange={() => {}}
        renderTurn={(turn) => <p>{turn.key}</p>}
      />
      <div ref={dockRef} />
    </VirtuosoMockContext.Provider>,
  );
  await waitFor(() => expect(screen.getByText("turn-199")).toBeInTheDocument());
  expect(screen.queryByText("turn-0")).not.toBeInTheDocument();
  expect(container.querySelectorAll("[data-turn-key]").length).toBeLessThan(20);
});

it("输入区滚轮转给聊天，长文本能自行滚动时保留原生滚动", async () => {
  const dockRef = createRef<HTMLDivElement>();
  const onFollowingChange = vi.fn();
  const { getByTestId } = render(
    <VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 200 }}>
      <ConversationTimeline
        turns={[{ key: "turn", log: false, user: null, process: [], active: null, final: null }]}
        listRef={createRef<VirtuosoHandle>()}
        wheelAreaRef={dockRef}
        following={false}
        onFollowingChange={onFollowingChange}
        renderTurn={() => <p>聊天正文</p>}
      />
      <div ref={dockRef} data-testid="dock"><textarea data-testid="draft" /></div>
    </VirtuosoMockContext.Provider>,
  );
  await screen.findByText("聊天正文");
  const viewport = getByTestId("virtuoso-scroller");
  viewport.scrollTop = 2000;
  const wheel = new WheelEvent("wheel", { deltaY: -120, bubbles: true, cancelable: true });
  getByTestId("dock").dispatchEvent(wheel);
  expect(wheel.defaultPrevented).toBe(true);
  expect(viewport.scrollTop).toBe(1880);
  expect(onFollowingChange).toHaveBeenCalledWith(false);

  const draft = getByTestId("draft");
  Object.defineProperties(draft, { scrollHeight: { value: 500 }, clientHeight: { value: 100 } });
  draft.scrollTop = 100;
  const textWheel = new WheelEvent("wheel", { deltaY: -40, bubbles: true, cancelable: true });
  draft.dispatchEvent(textWheel);
  expect(textWheel.defaultPrevented).toBe(false);
  expect(viewport.scrollTop).toBe(1880);
});

it("离开长会话再返回，恢复历史位置而不是回到开头或最新", async () => {
  const turns: TimelineTurn[] = Array.from({ length: 200 }, (_, index) => ({
    key: `restore-${index}`, user: null, process: [], active: null, final: null,
  }));
  function TestConversation({ following }: { following: boolean }) {
    const dockRef = createRef<HTMLDivElement>();
    return <VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 200 }}>
      <ConversationTimeline conversationKey="reading-restore" turns={turns}
        listRef={createRef<VirtuosoHandle>()} wheelAreaRef={dockRef}
        following={following} onFollowingChange={() => {}}
        renderTurn={(turn) => <p>{turn.key}</p>} />
      <div ref={dockRef} />
    </VirtuosoMockContext.Provider>;
  }
  const first = render(<TestConversation following />);
  await screen.findByText("restore-199");
  const viewport = screen.getByTestId("virtuoso-scroller");
  act(() => {
    viewport.scrollTop = 18000;
    viewport.dispatchEvent(new Event("scroll"));
  });
  first.unmount();
  render(<TestConversation following={false} />);
  await waitFor(() => expect(screen.getByTestId("virtuoso-scroller").scrollTop).toBe(18000));
});
