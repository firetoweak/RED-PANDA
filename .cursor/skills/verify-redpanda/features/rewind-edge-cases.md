# 回滚边界：压缩过的会话、带附件的会话

在已经 compact 过的会话上回滚，以及回滚包含附件的会话。基础回滚路径见 [file-rewind](./file-rewind.md)。来源：`redpanda/assistant/host/supervisor.py`（`restart_from_step`、`edit_and_fork`、`_lineage.supersede`）、`redpanda/assistant/host/session_store.py`（`fork_after_event`、`_materialize`）、`redpanda/assistant/compact/core.py`（`window_fact`、`CompactContext.refresh`、`compact_seed`）、`redpanda/assistant/attachments.py`（`AttachmentGateway.for_session`、`AttachmentStore.path`）、`redpanda/assistant/asset_locations.py`（`inherited_drawers`）、`redpanda/assistant/artifacts.py`。

源码事实（判据以此为准）：
- **回滚不是原地截断**：`restart_from_step` 先暂停源会话、`cancel_turn`、`wait_quiescent`，再用 `fork_after_event` 把「直到所选步骤那条事件（含）」的前缀复制成一个**新会话 id**，标记源会话被取代（`supersede`），新会话处于暂停态，等用户下一句话。源会话的 journal 与目录保留不删。
- 前缀里若有未解决的命令（`waiting_command_ids`），切分报 `SessionForkUnavailableError("branch prefix has unresolved commands")` → 409。
- **compact 状态完全由事件推出**：`window_fact(events)` 取最后一个 `WINDOW` 事实（检查 `parent` 链，断了抛 `broken context window lineage`）；`CompactContext.refresh` 据此加载窗口文档作为上下文前缀。所以：回滚点在 compact **之后** → 新会话继承该窗口；回滚点在 compact **之前** → 新会话没有窗口事实，用原始历史投影。旧版 `compact.continued` 事实会直接抛 `unsupported legacy compact continuation`。
- **附件按内容寻址**：图片 id 为 `sha256:<64 hex>`；每个会话的附件抽屉在 `runtime_sessions/<sha256(session_id)>/.attachments`，文件附件在其 `files/` 与共享 `.materials`。新会话的 `AttachmentStore.path` 先找自己的抽屉，再按 `inherited_drawers` 找来源会话的抽屉——**附件不复制，靠引用来源目录**。回滚掉的轮次上传的附件文件**不会被删除**（源码里没有附件清理逻辑）。
- 压缩窗口文档与工具产物同理通过 `FileArtifactGateway`（`artifacts.py`）的继承抽屉解析。

## Sub-features

- `rewind-after-compact`：回滚点晚于 compact。
- `rewind-before-compact`：回滚点早于 compact。
- `rewind-during-compact`：compact 正在进行时回滚。
- `rewind-keeps-attachment`：回滚点之前上传的附件在新会话仍可用。
- `rewind-drops-attachment`：回滚点之后上传的附件从对话中消失。
- `edit-with-attachment`：改写带附件的用户消息（`POST .../forks` 带 `artifact_refs`）。

## How to get to it (user POV)

- **压缩**：在 `模型配置` 把当前模型的 `compact 触发值` 调到很小（如 3000），开自动放行，交替发长消息与文件编辑，直到 journal 出现 compact 窗口事实（`sqlite3 journal.sqlite` 查 `fact_type` 为窗口的事件，或界面 `上下文用量` 回落）。在 compact 前后各做一次 `apply_patch`，各留一个暗号（compact 前说 `暗号一=ALPHA`，后说 `暗号二=BRAVO`）。
- **附件**：第 1 轮上传图片 `red.png` 并改文件，第 2 轮上传 `doc.txt` 并再改文件，然后在第 1 轮的步骤上 `从这一步之后重开`。

## Driving it with the browser

- **回滚点在 compact 之后**：
  - (a) 新会话 id ≠ 源会话 id；新会话 journal 中有与源会话相同的 `WINDOW` 事实链（数量相同、`parent` 链完整）；工作区文件等于该步的版本。
  - (b) `POST .../restarts` 201，地址跳到新会话，输入框显示暂停/等待输入。
  - (c) 源会话 journal 事件数不变、目录仍在；`GET /api/sessions/<源 id>` 仍可读（是否在列表里隐藏记录为观察项）。
  - (d) 问 `暗号一和暗号二分别是什么？` → 模型能答出 ALPHA（经窗口摘要或 `find_history`）与 BRAVO；若报 `broken context window lineage` 或会话起不来，判失败。
- **回滚点在 compact 之前**：
  - (a) 新会话 journal 中**没有** `WINDOW` 事实；工作区回到 compact 之前那一步的文件内容。
  - (b) 首次继续时上下文用量从原始历史重算（`已用` 数值可能比源会话 compact 后高），若立即超过阈值应再次 compact 而不是报 `模型输入超出上下文限制`。
  - (c) 源会话的 compact 产物不被删除或改写。
  - (d) 问暗号二 → 模型应答不知道（该信息在回滚点之后）；若能答出 BRAVO，说明回滚后的上下文泄漏了被丢弃的历史，判失败。
- **compact 进行中回滚**：触发 compact 后立刻点回滚。(a) 要么回滚等 compact 结束后完成，要么拒绝；新会话 journal 的窗口链完整。(d) 出现半个窗口事实、或新会话无法加载，判失败。源码里 `restart_from_step` 只调 `cancel_turn` + `wait_quiescent`，**没看到对 compact 读取任务（reader job）的专门处理**，此项为重点观察。
- **附件：回滚到第 1 轮**：
  - (a) 新会话转录里有 `red.png`，没有 `doc.txt`；`GET /api/sessions/<新 id>/attachments/<red 的 sha256 id>` 返回 200 且字节一致（解析自来源会话抽屉）；源会话抽屉里 `doc.txt` 文件**仍在磁盘上**。
  - (b) 新会话里 `read_image` 用 red 的 id 成功。
  - (c) 源会话中两个附件仍可下载；`runtime_sessions/` 下源抽屉内容不变。
  - (d) 在新会话用 `doc.txt` 的 id 请求下载或在 `inputs` 的 `artifact_refs` 引用它 → 记录实际结果。按源码，文件仍在来源抽屉、继承查找会命中，可能**仍能下载**（被回滚的附件在新会话里依然可读）；对于 `artifact_refs` 校验应返回 `附件不存在`。两者不一致时记为疑似问题。
- **改写带附件的消息**：在第 2 轮的用户消息上 `编辑消息`，删掉附件后 `改写并执行`。(a) 新会话该消息无 `doc.txt` 引用；模型回复不提及其内容。(d) 保留附件改写 → 附件 id 不变（内容寻址），下载字节一致。
- **来源会话被归档后**：归档源会话，再在新会话下载 red 附件。(a) 仍 200（`archive` 不删除 `runtime_sessions` 目录）。

## Gotchas

- **疑似问题：附件无回收。** 回滚/改写丢弃的轮次中的附件永远留在来源抽屉，长期占磁盘；且新会话可能还能按 id 读到它们。
- **疑似问题：新会话依赖来源目录。** 附件与压缩产物通过继承抽屉引用来源会话目录；若来源目录被手动或未来的清理逻辑删除，新会话的附件与窗口文档会读失败（`read` 注释：附件缺失视为持久化损坏，原样抛出）。
- 需要 `live_model_ready` 与 `sandbox.ready`；把 compact 阈值改回原值。
