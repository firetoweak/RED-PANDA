# 文件回滚（从这一步之后重开）

在保存过工作区版本的步骤上选 `从这一步之后重开`，再选 `会话和文件一起回滚`：服务端从那一步切出一个新会话（截短后的转录），工作区文件按 `preserve` 回到那一步；原会话被新会话取代、从列表隐藏。来源：`Conversation.tsx`（对话框与 `restart()`）、`ExecutionProcess.tsx`、`WorkspaceFileResultAlert.tsx`、`app.py` 的 `POST /api/sessions/{id}/restarts`（`restore_files`）、`redpanda/channels/web/channel.py` 的 `restart_from_step`、`redpanda/assistant/host/supervisor.py` 的 `_restart_from_step`、`redpanda/assistant/workspace_versions.py`（`settle_fork`、`WorkspaceFileResult`）、`redpanda/assistant/conversations.py`。

## Sub-features

- `rewind-open`：从可回滚的步骤（`step.rewindable`）点 `从这一步之后重开` 打开对话框，标题 `从这一步之后重开`，正文「对话会截到这一步，并从这里换一条走法。文件可以一起回滚，也可以保持现状。」，附注「文件回滚仅恢复工作区内已记录的受管修改。宿主命令、持续进程及工作区外操作产生的副作用不会撤销。」。
- `rewind-files`：`会话和文件一起回滚`（`restore_files: true`），按 `policy=preserve` 处理文件。
- `rewind-session-only`：`只回滚会话`（`restore_files: false`），文件不动；新会话 journal 写 `assistant.workspace_rescue`，`result.policy=keep`，`effect` 为 `kept`（分支点之后文件有变化）或 `unchanged`（无变化）；界面不显示提示，只有模型上下文收到说明。
- `rewind-new-session`：响应是**新会话**的 `ConversationView`（body 的 `session_id` 即新会话 id，没有 `child_session_id` 字段）；源会话标记为被取代，不再出现在 `GET /api/sessions` 列表中，映射写在 `$REDPANDA_HOME/runtime_sessions/lineage.json`。新会话处于暂停态，等用户下一句话。
- `rewind-file-result`：文件处理结果 `workspace_file_result` = `{policy, effect, changed_files, preserved_files, deleted_files, failed_files, error}`；`policy` 取 `preserve` / `original` / `keep`，`effect` 取 `restored` / `partially_restored` / `preserved` / `unchanged` / `kept` / `failed`。它出现在 `POST .../restarts` 响应和新会话的 `GET /api/sessions/<新 id>` 里，用户提交下一条消息后变为 `null`。
- `rewind-notice`：仅当 `effect` 为 `partially_restored` / `preserved` / `failed` 时显示 Alert `会话已回退 · 文件处理结果`（failed 红色，其余黄色），首行分别为「文件部分回退，部分后续值已保留」「后续值已保留，本次没有修改文件」「文件未能恢复；本次回退没有修改文件」，随后是 `error`（如有）和分组 `实际修改或删除（N）`、`其中已删除（N）`、`保留后续值（N）`、`未能处理（N）`，结尾「当前文件可能与保留的对话记录不同。……」。`restored` / `unchanged` / `kept` 不显示。用户在新会话提交下一条消息后提示消失。
- `rewind-disk`：磁盘上的字节真的恢复。

需要已经跑过 tool-coding-round 产生可回滚步骤，且 `sandbox.ready`。macOS 始终跳过。

## How to get to it (user POV)

- 完成 tool-coding-round，使 `note.txt` 为 `edited`，至少一个步骤显示 `从这一步之后重开`。
- 在 `read_file` 那一步点 `从这一步之后重开`，对话框里点 `会话和文件一起回滚`。

## Driving it with the browser

前置：同一会话；若 `read_file` 与 `apply_patch` 在同一行，先发 `请只用 apply_patch 把 note.txt 改成 later，然后回复 LATER_DONE。`，等文件为 `later` 后回滚那一行。

- **一起回滚**：
  - (a) `note.txt` 逐字节等于该步完成时的内容（回滚 `read_file` 行 → `verify`；回滚合并行 → `edited`）。用户在其间自己新建、助手未碰过的文件（如事先手动建 `user.txt`）仍在且内容不变。`GET /api/sessions/<新 id>`：所选步骤保留，其后的步骤消失；`workspace_file_result.policy=preserve`，`effect=restored`（或无变化时 `unchanged`）。`GET /api/sessions` 不再列出源会话；`lineage.json` 里新会话指向源会话。
  - (b) `POST .../restarts` 返回 201，body `session_id` ≠ 源会话 id；对话框关闭，地址以 replace 方式跳到 `/sessions/<新 id>`，之后的转录文字消失。`restored` 不弹提示。
  - (c) 所选步骤之前的消息（原始编码请求）保留；源会话 journal 与目录保留（`GET /api/sessions/<源 id>` 仍可读）；工作区外写入、`file_access=host` 写入、安装的依赖、网络调用不会被撤销（不作为失败）。
  - (d) 错误路径①：对没有成功版本记录的 `step_id` 直接 `POST .../restarts`（错误路径探测）→ 409，`detail` 为 `这一步没有成功的版本记录，无法从它之后重开：<step_id>`，不产生新会话。错误路径②：文件未能完全恢复时（例如人把助手改过的文件改成不同长度）→ 仍 201；`effect=failed`、`error` 为 `无法回退 <path>：…`、`failed_files` 含该路径；界面出现红色 `会话已回退 · 文件处理结果`，首行「文件未能恢复；本次回退没有修改文件」并列出 `未能处理`；不能只显示「回滚完成」。模型侧收到同一结果与「按用户目标核对当前文件」的引导。
- **部分保留**：助手改过、人原地同长改过的文件回滚 → (a) `effect` 为 `partially_restored` 或 `preserved`，`preserved_files` 含该文件。(b) 黄色提示，分组 `保留后续值` 列出它。(c) 人的字节保留。(d) 在新会话发一条消息后提示消失；`GET` 中 `workspace_file_result` 为 `null`。
- **只回滚会话**：(a) 新会话截短，`note.txt` 保持 `edited`；新会话 journal 有 `assistant.workspace_rescue`，`result.policy=keep`、`effect=kept`（无文件变化时 `unchanged`）。(b) 201，无提示。(c) 工作区所有文件不变。(d) 不能作为文件回滚通过的证据。
- **证据**：截短后的转录截图（带侧栏）、新旧会话 JSON、`lineage.json`、恢复后的 `note.txt` 存到 `$EVIDENCE_DIR/file-rewind/`。

## Gotchas

- 只有有成功文件版本的步骤才渲染回滚按钮，按钮缺失不是被隐藏。
- `只回滚会话` 不能证明本功能。
- 被判为只读的命令即便写了文件也不在回滚范围内；`file_access=host` 的写入同样不在范围内。
- 同一工作区其间其他会话的助手修改也可能被一起回退（见 agent-workspace-restore 中工具描述），多会话并行时要记录。
- 回滚后侧栏里只剩新会话；找源会话要用它的 id 直接 `GET`。
- 已知产品缺口（2026-10-18 源码核对）：`restart()` 请求失败时前端没有任何错误提示。对话框在请求前就关闭，`.unwrap()` 抛出的拒绝没有被捕获；输入框上方的 `操作失败` 提示只覆盖发送、改写、重试、分支和停止，不含重开（`Conversation.tsx` 中 `restart()` 与 `ErrorNotice` 条件）。正确预期：409 等失败应在界面上可见。验证错误路径①时界面看不到提示即属此缺口，记录而不判为通过。

### 成功条件补充

- 普通步骤回滚返回 201 是正常的，不是问题。
- 回滚不保证成功；失败时用户能在 `会话已回退 · 文件处理结果` 看到提示就算达标。
- 人手动（Web）回滚用 `preserve`：不覆盖人的改动，只回滚 agent 的部分。人机都改过的文件，界面须提示「此文件 agent 改过、人也改过，只回滚了 agent 的部分」；当前「文件部分回退，部分后续值已保留」未写清，记为剩余缺口。

### Live status

- 2026-10-18 源码核对（未 live）：修正响应字段（新会话 `session_id`，无 `child_session_id`）、源会话取代与列表隐藏、409 文案、`keep` 结果、提示的显示条件与文案、对话框文案；新增「重开失败无前端提示」产品缺口。
- 2026-10-16（main@e191eab，仅 API，界面未验证）：回滚 `read_file` 步 → 201 新会话 2 条、`effect: restored`、文件回到 `base`、用户文件不变；人机都改同一文件 → `effect: failed`+`error`（设计内可失败并提示）
- 通过（r2）：主路径 `AFTER_AGENT`→`before`，`restarts` 201（普通回滚 201 是正常结果）；失败与部分回退按新条件以界面提示为准
- **剩余缺口**：人机混合时的提示措辞（见上）
