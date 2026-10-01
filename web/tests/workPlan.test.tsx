import { MantineProvider } from "@mantine/core";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import type { WorkPlan } from "../src/api/contracts";
import { WorkPlanPanel } from "../src/features/conversation/WorkPlanPanel";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const plan: WorkPlan = {
  objective: "修复附件上传",
  steps: [
    { text: "定位原因", status: "completed" },
    { text: "修改逻辑", status: "in_progress" },
    { text: "验证重试", status: "pending" },
  ],
  note: "还需要验证重试",
};

it("shows only an active plan, starts collapsed and keeps recorded item counts distinct from completion", () => {
  vi.stubGlobal("matchMedia", () => ({
    matches: false, addEventListener() {}, removeEventListener() {},
  }));
  const { rerender } = render(<MantineProvider><WorkPlanPanel plan={null} /></MantineProvider>);
  expect(screen.queryByRole("button", { name: "当前计划" })).not.toBeInTheDocument();
  rerender(<MantineProvider><WorkPlanPanel plan={plan} /></MantineProvider>);
  const toggle = screen.getByRole("button", { name: "当前计划" });
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  expect(screen.getByText("已完成 1/3 项")).toBeInTheDocument();
  expect(screen.getByText("进行中：修改逻辑")).toBeInTheDocument();
  expect(screen.getByText("还需要验证重试")).toBeInTheDocument();
  expect(screen.queryByText("定位原因")).not.toBeInTheDocument();
  fireEvent.click(toggle);
  expect(screen.getByText("定位原因")).toBeInTheDocument();
  expect(screen.getByText("验证重试")).toBeInTheDocument();
  rerender(<MantineProvider><WorkPlanPanel plan={null} /></MantineProvider>);
  expect(screen.queryByRole("button", { name: "当前计划" })).not.toBeInTheDocument();
});
