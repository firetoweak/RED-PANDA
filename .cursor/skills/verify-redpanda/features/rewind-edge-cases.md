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

---

# 补充：人机混合修改的文件回滚

同一文件既有助手修改又有人手修改时，回滚（人点 `会话和文件一起回滚`，或模型调用 `restore_workspace`）怎么处理。来源：`native/sandbox/src/restore.rs`（`choose`、`Summary`）、`native/sandbox/src/restore/namespace.rs`（`reverse`、`plan`）、`redpanda/sandbox/files/operations.py`（`restore`、冲突原因表）、`redpanda/assistant/workspace_versions.py`（`settle_fork`、`restore`）、`redpanda/tools/builtin/workspace_restore.py`、`redpanda/assistant/context/prompt.py`、`redpanda/assistant/context/projection.py`、`web/src/features/conversation/Conversation.tsx`、设计文档 `docs/架构/Sandbox/文件操作与回退.md`。

源码事实（判据以此为准）：
- **归因按字节位置，不按作者**：文档：「归因依据是操作前后结果。记录助手改变的位置及其前后内容，而非监视自然人身份。」
- **底层只返回两组路径**：`restore_summary = {changed_paths, preserved_paths}`（`restore.rs` 的 `Summary`），没有每个文件的原因，也没有冲突清单。冲突时整个回退失败，只报**第一个**冲突路径与原因（`operations.py`：`无法回退 <path>：<原因>。`，原因如 `文件长度已变化，无法安全恢复`、`文件已被替换`、`文件类型已变化`、`目录包含需要保留的文件`），**不改动任何文件**。
- **长度检查先于策略**：`choose()` 中 `current.size != source.size` 直接 `length_changed` 冲突，preserve 和 original 都一样。
- **preserve**（模型默认，Web 回滚固定使用）：只撤销「当前值仍等于助手结果」的字节；人改过的字节保留并计入 `preserved_paths`。助手追加了尾部而人改了尾部 → 整个文件保持现状并计入 preserved。
- **original**：助手改过的字节一律恢复为助手修改前的值（覆盖人在这些位置上的修改）；助手没碰过的字节保留。只有模型调用 `restore_workspace` 时可选，Web 无此选项。
- **助手新建的文件**（namespace.rs `effect.before is None`）：preserve 下若当前内容≠助手写入内容 → 整个文件保留（blocked），计入 preserved；original 下 → 删除该文件（`Image::missing()`），人的修改一起删掉。文件身份变了（编辑器以「写新文件再改名」方式保存）→ `identity_changed` 冲突，整个回退失败。
- **模型能看到什么**：工具描述（`workspace_restore.py`）原文：「policy=preserve 保留用户后来改变的值；original 允许恢复助手改变位置的原值。两种策略都保留助手未改变的位置……成功结果说明实际改动的文件或目录，以及保留后续值的位置；同一路径可能同时出现。unchanged 表示本次没有改变文件，preserved 表示因保留后续值而未改变文件，不能据此宣称已完全撤销。」工具结果：`{"effect": "restored"|"partially_restored"|"preserved"|"unchanged", "changed_files", "preserved_files", "changed_count", "preserved_count", "truncated"}`；失败：`{"code": "WORKSPACE_RESTORE_FAILED", "error": "无法回退 ...", "hint": "本次回退没有修改文件；请核对相关文件的当前内容后再选择策略。"}`。系统提示（`prompt.py`）只说「文件回退只处理助手对工作区文件的修改；用户其他文件保持现状」。**没有任何文字指导模型对混合文件做语义合并、读 diff 或手工重做**。
- **人从 Web 回滚**：`Conversation.tsx` 的对话框只有说明「对话会截到这一步……工作区文件可以一起退回这一步，也可以保持现在的样子。」与两个按钮；`restart()` 收到响应后只做 `navigate(...)`。**没有预检、没有结果报告**。`settle_fork` 把结果写成领域事实：preserved 时 `assistant.workspace_carryover`（`文件回退保留了后续改动，当前文件可能与保留的对话记录不同；请读取相关文件核对。` + `files`），失败时 `assistant.workspace_rescue`（带 `error`）。这两个事实只经 `projection.py` 进入**模型**上下文（失败时显示为 `文件没有成功回退到所选位置，请核对当前文件后继续。`）；`redpanda/assistant/conversations.py` 与 `web/src/` 中都没有引用它们，**用户界面看不到**。回退失败时 `POST .../restarts` 仍返回 201，会话已截短而文件没动。

人的修改在各情况下的结局（按源码推导，待 live 证实）：

| 情况 | 人的修改长度不变（原位改字节） | 人的修改改变了文件长度 |
|---|---|---|
| A 助手与人都改了同一文件，preserve | 人改的字节**保留**；助手改的其余字节被撤销；文件可能变成语义上混杂的内容；计入 preserved | 整个回退**失败**，所有文件不动（`文件长度已变化，无法安全恢复`） |
| A，original（仅模型） | 人在助手改过的位置上的修改被**覆盖**；其他位置保留 | 同上，失败 |
| B1 助手新建、人后改，preserve | 文件**整体保留**（不删），计入 preserved | 同左（新建文件走 blocked 分支，不做长度检查） |
| B1，original（仅模型） | 文件**被删除**，人的修改一起丢失 | 同左，被删除 |
| B1，人用「另存再改名」方式保存 | `identity_changed`，整体失败 | 同左 |
| B2 人新建、助手后改，人没有再改 | 恢复成人写的版本（助手修改前的内容），人的内容**保留** | — |
| B2 人新建、助手改、人又改 | 同情况 A | 同情况 A |

## Sub-features

- `mixed-a-same-length`、`mixed-a-length-change`、`mixed-b1-preserve`、`mixed-b1-original`、`mixed-b2`、`mixed-web-report`（Web 回滚后是否告诉用户）、`mixed-model-report`（模型是否如实汇报 preserved）。

## How to get to it (user POV)

- 开自动放行，让助手 `apply_patch` 把 `mix.txt`（`hello world`）改成 `HELLO world`（长度不变），然后在 shell 里**原地**改：`printf 'HELLO WORLD' > mix.txt`（同长）或 `printf 'HELLO world!!' > mix.txt`（变长）。
- B1：让助手 `write_file` 新建 `agent.txt`（`abc`），人原地改为 `abX`。B2：人先建 `human.txt`，让助手改它。
- 然后分别：在助手那一步之前的步骤上点 `从这一步之后重开` → `会话和文件一起回滚`；或让助手 `请用 restore_workspace（policy=preserve / original）撤销刚才对这些文件的修改，并如实汇报结果`。

## Driving it with the browser + shell

- **A 同长 + Web 回滚**：(a) `mix.txt` 为 `hello WORLD`（人的大写 `WORLD` 保留，助手的 `HELLO` 撤销）；新会话 journal 有 `assistant.workspace_carryover`，`files` 含 `mix.txt`。(b) `POST .../restarts` 201。(c) 未涉及的文件不变。(d) **界面没有任何提示说 `mix.txt` 被部分保留** → 按源码预期如此，记为疑似问题（用户不知道文件处于混合状态）。
- **A 变长 + Web 回滚**：(a) 所有工作区文件（包括其他本该能回退的文件）**都没变**；新会话 journal 有 `assistant.workspace_rescue`，`error` 为 `无法回退 mix.txt：文件长度已变化，无法安全恢复。`。(b) 仍是 201，对话已截短、页面正常跳转。(c) 人的修改完整保留。(d) **界面不提示失败**——这就是此前发现的「回滚静默失败」形态之一，判为问题。
- **A + 模型 preserve**：(a) 同上的文件结果。(b) 工具结果 `effect` 为 `partially_restored` 或 `preserved`，`preserved_files` 含 `mix.txt`；变长时为 `WORKSPACE_RESTORE_FAILED` 和上述 `error`/`hint`。(d) 模型回复若宣称「已完全撤销」→ 判失败（违反工具描述）。
- **A + 模型 original**：(a) 同长时 `mix.txt` 变回 `hello world`，人的 `WORLD` 被覆盖；`preserved_files` 不含它。(d) 模型在选 original 前是否告知用户会覆盖人的修改——源码不要求，记录观察。
- **B1 preserve（Web 或模型）**：(a) `agent.txt` 仍为 `abX`，计入 preserved（模型结果 `preserved_files` / 新会话 carryover `files`）。(c) 文件未被删除。
- **B1 original（模型）**：(a) `agent.txt` **不存在**；结果 `changed_files` 含它。(d) 这是人的修改被删除的路径，确认模型回复是否提到；源码不阻止，记为风险。
- **B1 另存改名**：用 `cp agent.txt t && mv t agent.txt` 改内容后回滚 → (a) 整体失败，`error` 为 `无法回退 agent.txt：文件已被替换。`，其他文件也不回退。
- **B2**：人建 `human.txt`=`mine`，助手改为 `theirs`（同长不必），回滚 → (a) `human.txt`=`mine`。若人在助手之后又改，按 A 的判据。
- **撤销回退**：任一成功的模型回退后，让模型引用这次 `restore_workspace` 的 id 再撤销 → (a) 文件回到回退前的确切字节（含人的值，文档「撤销回退时恢复真正的执行前状态，包括当时存在的用户值」）。

## Gotchas

- **疑似问题：Web 回滚无预检、无结果报告。** 用户点 `会话和文件一起回滚` 前看不到哪些文件有人工修改，之后也看不到哪些被保留或回退失败；结果只写进模型上下文。
- **疑似问题：一个冲突导致整次回退失败，且只报第一个冲突。** 人对任何一个助手改过的文件做了变长修改，所有文件都不回退；HTTP 仍是 201。
- **疑似问题：长度变化就判冲突。** 人最常见的编辑（加一行）都会改长度，所以实际上「保留人工修改」只在原位同长修改时生效。
- **疑似问题：preserve 是字节级合并。** 同长的人工修改与助手修改交错时，结果可能是语义上不成立的混合内容，只以 preserved 路径提示，没有 diff。
- **疑似问题：没有引导模型做语义处理。** 工具描述与系统提示只要求如实汇报 changed/preserved，没有要求读 diff、征求用户、或手工重做。
- **original 会删除或覆盖人工修改**（B1 删除文件、A 覆盖同位置）；只能由模型选择，Web 不提供。
- 每个用例都在临时工作区里做，并用 `sha256sum` 记录前后哈希作为证据。

## Live status
- 部分（2026-10-12，main@1a4790a）：compact 前后回滚通过
- 已确认问题（证据路径相对 `/workspace/verify-run-20261012`）：
  - 回滚失败仍返回 201：`evidence/rewind-mixed/A_len/result.json`
  - 人机混改回滚拼接/丢弃：`evidence/rewind-mixed/`
  - 回滚后附件仍可下载、未清理：`evidence/rewind-edge-cases/attachments_after_rewind.json`
  - compact 中回滚：旧会话仍继续 compact（低）：`evidence/rewind-edge-cases/rewind_during_compact.json`
