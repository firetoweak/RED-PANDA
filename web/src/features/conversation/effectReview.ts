import type { EffectReview } from "../../api/contracts";

export const EFFECT_REVIEW_SAMPLE_STORAGE_KEY = "redpanda.experiment.effectReview";

const ACCEPT_PUBLISH_NOTE =
  "接受并发布还没有单独的操作入口。这个按钮不会调用工具，也不会写入会话。";

export function effectReviewSampleEnabled(
  search: string,
  storage: { getItem(key: string): string | null } | null,
): boolean {
  if (new URLSearchParams(search).get("effectReview") === "sample") {
    return true;
  }
  return storage?.getItem(EFFECT_REVIEW_SAMPLE_STORAGE_KEY) === "sample";
}

export function sampleEffectReview(step: {
  stepId: string;
  rewindable: boolean;
}): EffectReview {
  return {
    conclusion: "样例：启动耗时从 1.8s 降到 0.4s，改动集中在启动路径。",
    metrics: [
      { label: "启动耗时", before: "1.8", after: "0.4", unit: "s" },
      { label: "冷启动请求", before: "3", after: "1", unit: null },
    ],
    changes: [
      { path: "web/src/main.tsx", reason: "去掉重复的主题初始化" },
      { path: "redpanda/channels/web/app.py", reason: "静态资源只挂一次" },
    ],
    actions: [
      ...(step.rewindable
        ? [{ kind: "restore" as const, label: "从这一步之后重开", step_id: step.stepId }]
        : []),
      { kind: "todo" as const, label: "接受并发布", note: ACCEPT_PUBLISH_NOTE },
    ],
  };
}

// 已提交的 effect_review 直接用。样例只盖在已经收口、且投影里没有卡片的 step 上。
// 正在流式的正文 pending，这里返回 null，避免把未提交文字画成核对结果。
export function resolveEffectReview(
  step: {
    pending: boolean;
    stepId: string | null;
    rewindable: boolean;
    effectReview: EffectReview | null;
  } | null,
  options: { sample: boolean },
): { review: EffectReview; source: "projection" | "sample" } | null {
  if (step === null || step.pending) {
    return null;
  }
  if (step.effectReview !== null) {
    return { review: step.effectReview, source: "projection" };
  }
  const stepId = step.stepId;
  if (options.sample && stepId !== null) {
    return {
      review: sampleEffectReview({ stepId, rewindable: step.rewindable }),
      source: "sample",
    };
  }
  return null;
}
