import { MantineProvider } from "@mantine/core";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { TurnTiming } from "../src/features/conversation/TurnTiming";

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-10-03T08:00:05Z"));
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function timing(running: boolean, elapsedMs: number | null) {
  return (
    <MantineProvider>
      <TurnTiming startedAt="2026-10-03T08:00:00Z" running={running} elapsedMs={elapsedMs} />
    </MantineProvider>
  );
}

it("运行时从消息提交时间读秒，结束后采用记录耗时并停止计时", () => {
  const view = render(timing(true, null));
  expect(screen.getByText("正在执行 · 5 秒")).toBeInTheDocument();
  act(() => vi.advanceTimersByTime(2000));
  expect(screen.getByText("正在执行 · 7 秒")).toBeInTheDocument();

  view.rerender(timing(false, 6400));
  expect(screen.getByText("耗时 6.4 秒")).toBeInTheDocument();
  act(() => vi.advanceTimersByTime(5000));
  expect(screen.getByText("耗时 6.4 秒")).toBeInTheDocument();
  expect(vi.getTimerCount()).toBe(0);
});

it("退出运行且没有结束记录时不制造耗时，重新进入按原开始时间继续", () => {
  const view = render(timing(true, null));
  view.rerender(timing(false, null));
  expect(screen.queryByText(/正在执行|耗时/)).not.toBeInTheDocument();
  expect(vi.getTimerCount()).toBe(0);
  act(() => vi.advanceTimersByTime(10000));

  view.rerender(timing(true, null));
  expect(screen.getByText("正在执行 · 15 秒")).toBeInTheDocument();
  view.unmount();
  expect(vi.getTimerCount()).toBe(0);
});
