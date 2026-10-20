# 工作计划生命周期

模型用 `update_plan` 维护一份工作计划；界面在输入框上方显示 `当前计划`，全部完成后在该轮下方显示 `已完成计划`。本条覆盖创建、状态标记、更新、回收、完成五个阶段。来源：`redpanda/assistant/work_plan.py`、`redpanda/assistant/assembly.py`（注册 `update_plan`）、`redpanda/assistant/conversations.py`（`work_plan`、`work_plan_updates`）、`redpanda/assistant/context/projection.py`（`render_work_plan` 快照去重后追加到模型上下文末尾）、`redpanda/assistant/decision.py`（快照写入步骤 `decision_metadata` 的 `work_plan_context`）、`redpanda/assistant/context/prompt.py`（`WORK_PLAN_PROMPT`）、`web/src/features/conversation/WorkPlanPanel.tsx`、`workPlanPlacement.ts`、`Conversation.tsx`、`ExecutionProcess.tsx`、`stepHeading.ts`、`web/src/api/contracts.ts`（`workPlanSchema`）。

源码事实（判据以此为准）：
- **数据模型**（`work_plan.py`，pydantic `strict`、`extra="forbid"`）：`plan` 为 `{objective: 非空字符串, steps: [{text: 非空, status: "pending"|"in_progress"|"completed"}]（至少 1 项）, note: 非空字符串|null}` 或 `null`。事项**没有 id**，只按数组位置排列。前端 `workPlanSchema` 与之一致。
- **每次提交完整新版本**：工具描述原文（`work_plan.py` `UPDATE_PLAN_SPEC`）「为非简单、多阶段或长时间任务记录完整工作计划；简单任务无需调用。计划只保留简短目标、关键事项和必要说明，不写工作日志。每次提交完整新版本。note 写调整或阻塞说明，没有说明时显式传 null。目标变化时更新或替换计划；plan=null 表示结束当前计划，历史仍保留。」所以「增删改排序」都只是提交一份新数组，服务端不做差分。
- **工具结果**：`{"ok": true, "code": "PLAN_UPDATED", "data": {"plan": ...}}`。参数不合法时由 `ToolsExecutor` 返回校验错误，不产生计划。
- **计划的唯一事实来源**：`_plan_updates` 只取「已提交步骤中的 `update_plan` 命令」且 outcome `SUCCEEDED` 且 `ok: true` 的结果，按到达顺序；**最后一条即当前计划**（`project_work_plan`）。待授权、失败、被拒绝、未完成的调用不算。
- **没有状态迁移规则**：源码不校验 `pending→in_progress→completed` 的顺序，允许多个 `in_progress`，允许 `completed→pending` 倒退，允许直接提交全 `completed`。
- **用户没有计划操作入口**：`更新计划` / `结束当前计划` 只是步骤标题（`stepHeading.ts`、`ExecutionProcess.tsx`：`tool.arguments.plan === null ? "结束当前计划" : "更新计划"`），不是按钮。面板唯一的交互是展开/收起（`aria-label` 为 `当前计划` 或 `已完成计划`）。
- **界面放置**：`Conversation.tsx` 中 `activePlan` 只在当前计划存在且**未全部完成**时显示在输入框上方；全部完成的计划由 `completedPlanForTurn` 挂在产生它的那一轮下方，标题 `已完成计划`。
- **面板文字**（`WorkPlanPanel.tsx`）：标题 `当前计划 · <objective>`（或 `已完成计划 · <objective>`）；其下固定一行 `模型记录的计划进度`，只有输入框上方的当前计划会传 `stopped`（未运行且会话已暂停或有错误），此时这一行为 `模型记录的计划进度 · 执行已停止`；有 `in_progress` 项时再一行 `计划标记为进行中：<各项>`（以 `、` 连接）；右侧 `已完成 n/m 项`。有 `note` 时 note 显示在标题按钮下方，不用展开；展开后每项前标 `待做`/`进行中`/`已完成`。
- **模型上下文**：`render_work_plan(events)` 从 journal 事件算出快照，有计划时为 `<work_plan>\n{JSON}\n</work_plan>`，`plan=null` 后为 `<work_plan>\n当前计划已清除。\n</work_plan>`；块里没有说明文字，含义由系统提示 `WORK_PLAN_PROMPT` 解释（「最近一块是当前计划，更早的只是历史；最近一块说明已清除时，不再沿用旧计划。」）。快照**只在与当前窗口内最近一块 `<work_plan>` 不同时**才追加到消息末尾，并存进该步骤 `decision_metadata` 的 `work_plan_context`，之后作为窗口里的历史消息重放（`projection.py` `_project_step`）。计划没变时后续决策不再追加；compact 后旧块滚出窗口，下一次决策会重新追加一次。

## Sub-features

- `create`：首次 `update_plan` 产生计划。
- `mark`：修改事项状态。
- `update`：增、删、改名、重排事项；改 objective/note；整份替换。
- `recycle`：模型 `plan=null` 结束；停止/取消/compact/新会话/子 Agent 时计划的去向。
- `complete`：全部 `completed` → `已完成计划` 归档到该轮。

## How to get to it (user POV)

- 发：`请先用 update_plan 建一个三步计划（目标“验证计划”，第一项进行中），然后每完成一步就 update_plan 更新状态，每步只回复一句话。`
- 其他阶段通过在消息里明确要求模型怎么调用 `update_plan` 来驱动（模型是唯一写入方）。错误路径可以让模型故意传错参数，或用 `pytest` 直接调 `UPDATE_PLAN_SPEC`。

## Driving it with the browser

每个阶段用 `curl $REDPANDA_VERIFY_URL/api/sessions/<id>` 读 `work_plan`（当前）与 `work_plan_updates`（历史，`[{step_id, plan}]`）作为状态证据。「当前窗口内最近一块 `<work_plan>`」用 journal 中各决策步骤 `decision_metadata.work_plan_context` 核对：取最近一个带该键的步骤（compact 后只看新窗口内的步骤）。

- **创建**：
  - (a) `work_plan.objective` 为 `验证计划`，`steps` 3 项，第 1 项 `in_progress`；`work_plan_updates` 长度 1，`step_id` 指向含该调用的步骤。
  - (b) 步骤标题 `更新计划`；输入框上方出现 `当前计划 · 验证计划`、`模型记录的计划进度`、`计划标记为进行中：<第一项>`、`已完成 0/3 项`。之后的决策步骤里，当前窗口内最近一块 `<work_plan>` 是 `<work_plan>\n{该版本 JSON}\n</work_plan>`。
  - (c) 不写工作区文件；不需要授权（不出现 `待授权`）。
  - (d) 空计划：让模型传 `steps: []` → 工具返回校验错误，`work_plan` 不变（仍为上一版或 `null`）。`objective: ""`、`status: "done"`、多余字段（如 `id`）、`note` 省略（未显式传 `null`）→ 同样校验失败、计划不变。
- **状态标记**：
  - (a) 每次更新后 `work_plan` 的状态与模型最后一次成功调用一致；`work_plan_updates` 递增。
  - (b) `已完成 n/m 项` 与 `计划标记为进行中：` 随之变化；展开后每项标签正确。
  - (c) 历史版本（`work_plan_updates` 中较早的项）不被改写。
  - (d) 让模型同时标两项 `in_progress` → **被接受**，那一行以 `、` 列出两项；让模型把已完成项改回 `pending` → **被接受**。计划由模型控制、没有完整状态机，这是设计，判通过。
- **更新**（增/删/改名/重排/替换）：
  - (a) 新增第 4 项、删除第 2 项、改第 1 项文字、颠倒顺序、换一个 objective 的整份新计划——每次 `work_plan` 都**完整等于**最后提交的版本，旧事项不残留。
  - (b) 面板 `m` 变化；objective 变化时标题随之变化；`note` 在标题按钮下方出现/消失（不用展开）。
  - (c) 事项无 id，界面以数组下标作 key（`WorkPlanPanel.tsx` 的 `key={index}`）；重排后展开列表顺序必须与 JSON 一致。
  - (d) 计划已全部完成后再 `update_plan` 一份新计划 → 接受，新计划成为当前计划显示在输入框上方，旧的 `已完成计划` 仍挂在原轮下方。
- **回收：模型结束计划**：让模型 `update_plan(plan=null)`。(a) `work_plan` 为 `null`；`work_plan_updates` 末项 `plan: null`，早先版本仍在。(b) 步骤标题 `结束当前计划`；输入框上方面板消失；之后的决策步骤里，当前窗口内最近一块 `<work_plan>` 是 `<work_plan>\n当前计划已清除。\n</work_plan>`。(d) 未完成的计划被 `null` 结束后**界面不留任何「已放弃」记录**（`completedPlanForTurn` 只挂全部完成的计划）——记为观察项。
- **回收：用户想结束计划**：界面没有按钮。用户只能发消息要求模型结束。(d) 记为疑似问题：用户无法直接放弃或清除计划。
- **回收：停止/取消**：计划有 `in_progress` 项时点输入框的 `停止`。(a) `work_plan` 保持最后一次成功版本，没有自动清理。(b) 面板仍显示 `计划标记为进行中：...`，执行停止后那一行显示 `模型记录的计划进度 · 执行已停止`；实际运行状态单独显示，不靠计划推断。(d) 停止发生在 `update_plan` 已开始但无结果时 → 该调用不计入（只认 `SUCCEEDED`），`work_plan` 为上一版。
- **回收：失败或被拒绝的更新**：(a) 工具失败/校验错误的调用不改变 `work_plan`。
- **回收：compact**：建计划后把 compact 阈值调小触发 compact。(a) compact 后 `work_plan` 不变（来源是 journal 事件）。(b) compact 后的第一次决策步骤带 `work_plan_context`，当前窗口内最近一块 `<work_plan>` 是 compact 前的当前版本；问模型「现在计划的第二项是什么」能答对。(d) 若新窗口里没有任何 `<work_plan>` 块，判失败。
- **回收：另一个 Agent**：在同一项目再 `新建 Agent` → 新会话的 `work_plan` 为 `null`，面板不显示；计划不跨 Agent 继承。
- **子 Agent 的计划**：让父委派一个子 Agent 并要求子也用 `update_plan`。(a) 子会话 `GET /api/sessions/<子 id>` 有自己的 `work_plan`；父的 `work_plan` 不变。(b) 子计划不出现在父的 `当前计划`；点子 Agent 调用卡打开的 `子 Agent` 页（`ContentPage` 内的 `SubagentPanel`）不渲染计划，子计划只能经子会话的 `GET` 读到。(c) 子被收回/交回后父计划不受影响。
- **完成与归档**：模型把所有项标 `completed`。(a) `work_plan` 全为 `completed`。(b) 输入框上方面板消失；该轮下方出现 `已完成计划 · <objective>`、`模型记录的计划进度`（不带 `执行已停止`）、`已完成 m/m 项`。(c) 当前窗口内最近一块 `<work_plan>` 是这份全部完成的版本（全部完成的计划不会自动清除，直到模型提交 `null` 或新计划）。(d) 同一轮内先完成计划 A、再建并完成计划 B → 该轮下方只显示最后一份完成的计划（`completedPlanForTurn` 取最后一次），A 不再显示——记为观察项。

## Gotchas

- 设计行为：没有状态迁移校验。多个 `in_progress`、倒退、跳过都被接受；计划是模型上次提交的进度记录，界面标明 `模型记录的计划进度`，实际运行状态单独显示。
- 设计行为：停止后 `in_progress` 原样保留。停止、失败、回合取消都不清理计划，同时显示 `模型记录的计划进度 · 执行已停止`；后续由模型自己更新计划。
- **疑似问题：用户无法直接结束或清除计划。** `结束当前计划` 只是步骤标题；用户只能请求模型做。
- **疑似问题：放弃的计划不留痕。** `plan=null` 结束未完成计划后，界面上它直接消失，只有展开步骤才能看出；不像完成计划那样归档。
- **疑似问题：同轮多份完成计划只显示最后一份。**
- 快照只在变化时追加，所以「每次请求末尾都有计划」不是判据；看当前窗口内最近一块。全部完成的计划快照留在窗口里占用 token，直到被替换或清除。
- 计划完全依赖模型调用；模型可能不调用 `update_plan`。通过条件是 journal 中真的有成功的 `update_plan` 结果。
- 需要 `live_model_ready`。
- 成功条件：计划由模型制定和控制，没有完整状态机。双 `in_progress`、停止后仍是 `in_progress`、倒退都不算问题；只看计划能创建、更新、显示，运行状态单独显示。
- 历史验证：r2 按新条件通过（cancel 后第 1 步仍 `in_progress`、可双 `in_progress`，均为设计行为）；UI 截图里计划与运行状态分开显示未验。2026-10-18 按源码改写文案与上下文判据，未 live 复跑。

- 2026-10-20 维护实测（`origin/main@32860f6`，Windows Chrome/ConPTY，隔离 home）：update_plan 建两步计划，API 留有进行中和全部完成两版；页面已完成计划 2/2 可展开。结束、compact、停止与子计划回收未重验。
