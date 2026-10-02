import type { ConversationView, WorkPlan } from "../../api/contracts";
import type { TimelineTurn } from "./timelineTurns";

export function isPlanCompleted(plan: WorkPlan): boolean {
  return plan.steps.every((step) => step.status === "completed");
}

export function completedPlanForTurn(
  turn: TimelineTurn,
  updates: ConversationView["work_plan_updates"],
): WorkPlan | null {
  const stepIds = new Set(
    [...turn.process, turn.active, turn.final]
      .filter((step) => step !== null)
      .map((step) => step.stepId),
  );
  let completed: WorkPlan | null = null;
  for (const update of updates) {
    if (stepIds.has(update.step_id) && update.plan !== null && isPlanCompleted(update.plan)) {
      completed = update.plan;
    }
  }
  return completed;
}
