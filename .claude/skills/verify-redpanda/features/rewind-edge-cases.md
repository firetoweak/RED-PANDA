# 回滚边界：压缩过的会话、带附件的会话、人机混合修改的文件

在已经 compact 过的会话上回滚、回滚或改写包含附件的会话，以及同一文件既有助手修改又有人手修改时的回滚（人点 `会话和文件一起回滚`，或模型调用 `restore_workspace`）。基础回滚路径见 [file-rewind](./file-rewind.md)，模型回退的结果形状见 [agent-workspace-restore](./agent-workspace-restore.md)。

来源：`redpanda/assistant/host/supervisor.py`（`_restart_from_step`、`fork_and_accept_input`、`_lineage.supersede`）、`redpanda/assistant/host/session_store.py`（`fork_after_event`、`_materialize`）、`redpanda/runtime/journal/sqlite.py`（`create_branch`、`history_spans`）、`redpanda/assistant/compact/core.py`（`WINDOW`、`window_fact`、`compact_seed`）、`redpanda/assistant/compact/host.py`（`retire`）、`redpanda/assistant/attachments.py`（`available_in_session`、`is_valid_attachment_id`）、`redpanda/assistant/file_attachments.py`、`redpanda/assistant/asset_locations.py`、`redpanda/assistant/artifacts.py`、`redpanda/channels/web/channel.py`（`attachment_file`、`_require_refs`）；混合修改部分另见 `native/filesystem/src/restore.rs`（`choose`、`Summary`）、`native/filesystem/src/restore/namespace.rs`、`redpanda/filesystem/operations.py`（`restore`、冲突原因表）、`redpanda/assistant/workspace_versions.py`（`settle_fork`、`restore`、`workspace_file_result_note`）、`redpanda/assistant/context/projection.py`、`web/src/features/conversation/WorkspaceFileResultAlert.tsx`、设计文档 `docs/架构/Agent文件系统/文件操作与回退.md`。

## Sub-features

### 压缩与附件

- `rewind-after-compact`：回滚点晚于 compact。
- `rewind-before-compact`：回滚点早于 compact。
- `rewind-during-compact`：compact 正在进行时回滚。
- `rewind-keeps-attachment`：回滚点之前上传的附件在新会话仍可用。
- `rewind-drops-attachment`：回滚点之后上传的附件从新会话中消失。
- `edit-with-attachment`：改写带附件的用户消息（`POST .../forks` 带 `artifact_refs`）。

### 人机混合修改

- `mixed-a-same-length`、`mixed-a-length-change`、`mixed-b1-preserve`、`mixed-b1-original`、`mixed-b2`、`mixed-web-report`（Web 回滚后是否告诉用户）、`mixed-model-report`（模型是否如实汇报 preserved）。

### 源码事实：回滚分叉与 compact（判据以此为准）

- **回滚不是原地截断**：`_restart_from_step` 先校验所选步骤有成功的版本记录，然后暂停源会话、`compact.retire(源会话)`（停掉该会话正在进行的 compact 读取任务并等其 Worker 退出）、`cancel_turn`、`wait_quiescent`，再用 `fork_after_event` 切出一个**新会话 id**，标记源会话被取代（`supersede`），新会话处于暂停态，等用户下一句话。源会话的 journal 与目录保留不删。
- **分支前缀是引用，不是复制**：`_materialize` → `create_branch` 只在新 journal 的 `history_spans` 表里登记「源 journal 第 1..N 条」并在 `asset_sources` 里登记来源 journal；前缀事件仍只存在于源 journal。因此直接 `sqlite3 <新会话>/journal.sqlite` 查 `events` 看不到前缀里的事实（包括窗口事实），要先读 `history_spans` 再去对应源 journal 查 `sequence ≤ last_sequence` 的事件；`GET /api/sessions/<新 id>` 读的是合并后的完整历史。若源会话目录被删除，新会话的整段继承历史都读不到。
- 前缀里若有未解决的命令（`waiting_command_ids`），切分报 `SessionForkUnavailableError("branch prefix has unresolved commands")` → 409。
- **compact 状态完全由事件推出**：窗口事实类型为 `compact.window_rolled_over`；`window_fact(events)` 取最后一个窗口事实并检查 `parent` 链，断了抛 `broken context window lineage`。回滚点在 compact **之后** → 新会话历史里含该窗口事实，继承窗口；回滚点在 compact **之前** → 没有窗口事实，用原始历史投影。旧版 `compact.continued` 的专门检查已删除（15fcb37）；`compact_seed` 只校验 compact 任务种子必须紧跟唯一的工作区绑定。
- `GET /api/sessions/<id>` 的 `compact_count` 只统计**以该会话为来源**已发布的 compact 次数（`compact/store.py`），新会话刚切出时为 0，即使它继承了窗口；它能证明源会话做过 compact，不能证明继承。
- **附件**：图片 id 为 `sha256:<64 hex>`（内容寻址）；文件附件 id 为 `file:<32 hex>`（`uuid4`，随机，同一文件再次上传得到不同 id）。新会话按 `inherited_drawers` 找来源会话的抽屉——**附件不复制，靠引用来源目录**。回滚掉的轮次上传的附件文件**不会被删除**（源码里没有附件清理逻辑）。可读性由 `available_in_session` 判定：本会话抽屉拥有的附件，或本会话（含继承前缀）历史里实际引用过的附件；被回滚掉的轮次不在新会话历史里，因此不可读。
- 压缩窗口文档与工具产物同理通过 `FileArtifactGateway`（`artifacts.py`）的继承抽屉解析。

### 源码事实：人机混合修改（判据以此为准）

- **归因按字节位置，不按作者**：文档：「归因依据是操作前后结果。记录助手改变的位置及其前后内容，而非监视自然人身份。」
- **底层只返回两组路径**：`restore_summary = {changed_paths, preserved_paths}`（`restore.rs` 的 `Summary`）；Python 侧另从回执中 `after.kind == "missing"` 的变化推出 `deleted_paths`（`operations.py`）。没有每个文件的原因，也没有冲突清单。冲突时整个回退失败，只报**第一个**冲突路径与原因（`无法回退 <path>：<原因>。`），**不改动任何文件**。原因表（`operations.py`）：`structure_changed` 文件类型已变化、`identity_changed` 文件已被替换、`length_changed` 文件长度已变化，无法安全恢复、`content_unavailable` 缺少恢复所需的原文、`directory_contains_user_children` 目录包含需要保留的文件、`unsupported_directory_replacement` 不支持恢复该目录替换、`unsupported_directory_rename` 不支持恢复该目录重命名、`parent_changed` 上级目录已变化、`unsupported_link_restore` 不支持恢复该链接变化、`unsupported_rename_cycle` 不支持恢复该组相互重命名。
- **长度检查先于策略**：`choose()` 中（除纯元数据变化外）`current.size != source.size` 直接 `length_changed` 冲突，preserve 和 original 都一样。
- **preserve**（模型默认，Web 回滚固定使用）：只撤销「当前值仍等于助手结果」的字节；人改过的字节保留并计入 `preserved_paths`。
- **original**：助手改过的字节一律恢复为助手修改前的值（覆盖人在这些位置上的修改）；助手没碰过的字节保留。只有模型调用 `restore_workspace` 时可选，Web 无此选项。
- **助手新建的文件**（namespace.rs `effect.before.is_none()`）：preserve 下若当前内容≠助手写入内容 → 整个文件保留（blocked），计入 preserved；original 下 → 删除该文件，人的修改一起删掉。文件身份变了（编辑器以「写新文件再改名」方式保存）→ `identity_changed` 冲突，整个回退失败。
- **模型能看到什么**：`restore_workspace` 的描述与结果见 [agent-workspace-restore](./agent-workspace-restore.md)（「unchanged 或 preserved 表示没有完全撤回」「不要换 policy 反复重试」；结果 `data{policy, effect, changed_files, preserved_files, deleted_files, failed_files, error}` + `hint`；失败 `WORKSPACE_RESTORE_FAILED` + `failed_files` + 以「本次回退没有修改文件。」开头的 `hint`）。系统提示（`prompt.py`）已不再提回退。
- **人从 Web 回滚**：对话框文案见 file-rewind，没有预检。`settle_fork` 把结果统一写成领域事实 `assistant.workspace_rescue`，数据 `{step_id, before_version, version, error, result}`，`result` 为 `WorkspaceFileResult`（没有 `assistant.workspace_carryover` 这种事实）。这条事实有两份投影：
  - 模型：`projection.py` 把 `result` 加上 `note`（`workspace_file_result_note`）放进上下文，如 failed「会话已回退，文件未能恢复；本次文件回退没有修改文件。」、partially_restored「文件已部分回退，同时保留了后续值；当前文件可能与对话中看到的内容不同。」、preserved「文件回退保留了后续值，本次没有修改文件；……」、kept「对话已从较早位置重新开始，但工作区文件没有退回；……」，末尾统一接「按用户目标核对当前文件，必要时用文件工具修正；不要反复重试。」。
  - 用户：`conversations.py` 投影为 `workspace_file_result`，`Conversation.tsx` 渲染 `WorkspaceFileResultAlert`（`会话已回退 · 文件处理结果`），仅 `partially_restored` / `preserved` / `failed` 显示，提交下一条消息后消失。
  回退失败时 `POST .../restarts` 仍返回 201，新会话已截短而文件没动。

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

## How to get to it (user POV)

- **压缩**：在 `模型配置` 把当前模型的 `compact 触发值` 调到很小（如 3000），开自动放行，交替发长消息与文件编辑，直到源会话 `GET` 的 `compact_count` ≥ 1（或界面 `上下文用量` 回落）。在 compact 前后各做一次 `apply_patch`，各留一个暗号（compact 前说 `暗号一=ALPHA`，后说 `暗号二=BRAVO`）。
- **附件**：第 1 轮上传图片 `red.png` 并改文件，第 2 轮上传 `doc.txt` 并再改文件，然后在第 1 轮的步骤上 `从这一步之后重开`。
- **混合修改**：开自动放行，让助手 `apply_patch` 把 `mix.txt`（`hello world`）改成 `HELLO world`（长度不变），然后在 shell 里**原地**改：`printf 'HELLO WORLD' > mix.txt`（同长）或 `printf 'HELLO world!!' > mix.txt`（变长）。B1：让助手 `write_file` 新建 `agent.txt`（`abc`），人原地改为 `abX`。B2：人先建 `human.txt`，让助手改它。然后分别：在助手那一步之前的步骤上点 `从这一步之后重开` → `会话和文件一起回滚`；或让助手 `请用 restore_workspace（policy=preserve / original）撤销刚才对这些文件的修改，并如实汇报结果`。

## Driving it with the browser + shell

### 压缩

- **回滚点在 compact 之后**：
  - (a) 新会话 id ≠ 源会话 id；新会话 `history_spans` 指向源 journal，且源 journal 中 `sequence ≤ last_sequence` 范围内有 `compact.window_rolled_over` 事实（`select sequence, payload_json from events where payload_json like '%compact.window_rolled_over%'`），`parent` 链完整；新会话自己的 `events` 表里看不到这些事实属正常。工作区文件等于该步的版本。
  - (b) `POST .../restarts` 201，地址跳到新会话，输入框显示暂停/等待输入。
  - (c) 源会话 journal 事件数不变、目录仍在；`GET /api/sessions/<源 id>` 仍可读，但源会话不再出现在 `GET /api/sessions` 列表里。
  - (d) 问 `暗号一和暗号二分别是什么？` → 模型能答出 ALPHA（经窗口摘要或 `find_history`）与 BRAVO；若报 `broken context window lineage` 或会话起不来，判失败。
- **回滚点在 compact 之前**：
  - (a) 新会话引用的前缀范围内**没有** `compact.window_rolled_over` 事实；工作区回到 compact 之前那一步的文件内容。
  - (b) 首次继续时上下文用量从原始历史重算（`已用` 数值可能比源会话 compact 后高），若立即超过阈值应再次 compact 而不是报 `模型输入超出上下文限制`。
  - (c) 源会话的 compact 产物不被删除或改写。
  - (d) 问暗号二 → 模型应答不知道（该信息在回滚点之后）；若能答出 BRAVO，说明回滚后的上下文泄漏了被丢弃的历史，判失败。
- **compact 进行中回滚**：触发 compact（源会话 `compact_phase` 为 `running`）后立刻点回滚。(a) 回滚先 `compact.retire` 源会话的读取任务并等待其 Worker 退出，再切分支；新会话前缀里的窗口链完整，源会话该次 compact 不发布（`compact_count` 不因它增加）。(b) 仍 201。(c) 源会话已发布的窗口不变。(d) 出现半个窗口事实、新会话无法加载、或回滚请求挂住不返回，判失败。

### 附件

- **回滚到第 1 轮**：
  - (a) 新会话转录里有 `red.png`，没有 `doc.txt`；`GET /api/sessions/<新 id>/attachments/<red 的 sha256 id>` 返回 200 且字节一致（解析自来源会话抽屉）；源会话抽屉里 `doc.txt` 文件**仍在磁盘上**。
  - (b) 新会话里 `read_image` 用 red 的 id 成功。
  - (c) 源会话中两个附件仍可下载；`runtime_sessions/` 下源抽屉内容不变。
  - (d) 在新会话用 `doc.txt` 的 `file:<32 hex>` id 请求下载 → 404，`detail` 为 `附件不属于当前会话。`；在新会话 `inputs` 的 `artifact_refs` 引用它 → 400，`detail` 为 `附件不属于当前会话`。格式不合法的 id 在 `artifact_refs` 里 → 422（请求校验）。
- **改写带附件的消息**：在第 2 轮的用户消息上 `编辑消息`，点附件上的 `Remove doc.txt` 删掉后 `改写并执行`。(a) 新会话该消息无 `doc.txt` 引用；模型回复不提及其内容。(d) 保留图片附件改写 → 图片 id 不变（内容寻址），下载字节一致；文件附件沿用原 `file:` id（不是重新上传）。
- **来源会话被归档后**：归档源会话，再在新会话下载 red 附件。(a) 仍 200（`archive` 不删除 `runtime_sessions` 目录）。

### 人机混合修改

- **A 同长 + Web 回滚**：(a) `mix.txt` 为 `hello WORLD`（人的大写 `WORLD` 保留，助手的 `HELLO` 撤销）；新会话 `workspace_file_result.effect` 为 `partially_restored` 或 `preserved`，`preserved_files` 含 `mix.txt`；新会话 journal 有 `assistant.workspace_rescue`。(b) `POST .../restarts` 201。(c) 未涉及的文件不变。(d) 界面 `会话已回退 · 文件处理结果` 在 `保留后续值` 下列出 `mix.txt` → 按当前成功条件需说明只回退了 agent 的部分；字节混合可接受，**缺这条提示才算失败**（当前措辞缺口见 Gotchas）。
- **A 变长 + Web 回滚**：(a) 所有工作区文件（包括其他本该能回退的文件）**都没变**；`workspace_file_result.effect=failed`、`error` 为 `无法回退 mix.txt：文件长度已变化，无法安全恢复。`、`failed_files=["mix.txt"]`。(b) 仍是 201，对话已截短、页面正常跳转。(c) 人的修改完整保留。(d) 界面显示红色提示「文件未能恢复；本次回退没有修改文件」、`error` 与 `未能处理` → 通过；看不到失败提示才算问题。
- **A + 模型 preserve**：(a) 同上的文件结果。(b) 工具结果 `data.effect` 为 `partially_restored` 或 `preserved`，`data.preserved_files` 含 `mix.txt`；变长时为 `WORKSPACE_RESTORE_FAILED`、上述 `error`、`failed_files` 与 `hint`。(c) 历史不截短。(d) 模型回复若宣称已经完全撤回 → 判失败（违反工具描述）。
- **A + 模型 original**：(a) 同长时 `mix.txt` 变回 `hello world`，人的 `WORLD` 被覆盖；`preserved_files` 不含它。(b) `hint` 含「policy=original 可能覆盖或删除用户后来的修改，请核对。」。(d) 模型在选 original 前是否告知用户会覆盖人的修改——源码不要求，记录观察。
- **B1 preserve（Web 或模型）**：(a) `agent.txt` 仍为 `abX`，计入 `preserved_files`。(c) 文件未被删除。
- **B1 original（模型）**：(a) `agent.txt` **不存在**；结果 `changed_files` 与 `deleted_files` 都含它。(d) 这是人的修改被删除的路径，确认模型回复是否提到；源码不阻止，记为风险。
- **B1 另存改名**：用 `cp agent.txt t && mv t agent.txt` 改内容后回滚 → (a) 整体失败，`error` 为 `无法回退 agent.txt：文件已被替换。`，其他文件也不回退。
- **B2**：人建 `human.txt`=`mine`，助手改为 `theirs`，回滚 → (a) `human.txt`=`mine`。若人在助手之后又改，按 A 的判据。
- **撤销回退**：任一成功的模型回退后，让模型引用这次 `restore_workspace` 的 id 再撤销 → (a) 文件回到回退前的确切字节（含人的值，文档「撤销回退时恢复真正的执行前状态，包括当时存在的用户值」）。

## Gotchas

- **疑似问题：附件无回收。** 回滚/改写丢弃的轮次中的附件永远留在来源抽屉，长期占磁盘（新会话按 id 已读不到它们）。
- **疑似问题：新会话依赖来源目录。** 前缀历史、附件与压缩产物都通过引用来源会话目录取得；若来源目录被手动或未来的清理逻辑删除，新会话的整段继承历史、附件与窗口文档都会读失败（附件缺失视为持久化损坏，原样抛出）。
- 检查继承的窗口事实不要只查新会话的 `journal.sqlite`，也不要用新会话的 `compact_count`；两者都看不到继承部分。
- 需要 `live_model_ready` 与 `sandbox.ready`；把 compact 阈值改回原值。
- **疑似问题：Web 回滚无预检。** 用户点 `会话和文件一起回滚` 前看不到哪些文件有人工修改；回滚后的结果已有 `会话已回退 · 文件处理结果` 提示（仅部分保留与失败时显示）。
- 设计行为：一个冲突导致整次回退失败、只报第一个冲突；只要用户看得到失败提示即可。
- **疑似问题：长度变化就判冲突。** 人最常见的编辑（加一行）都会改长度，所以实际上「保留人工修改」只在原位同长修改时生效。
- 设计行为：preserve 是字节级保留人的改动；有「agent 和人都改过、只回滚了 agent 的部分」的提示即可。
- 设计行为：runtime 只如实报告 changed/preserved/deleted/failed，语义处理交给模型。
- **original 会删除或覆盖人工修改**（B1 删除文件、A 覆盖同位置）；只能由模型选择，Web 不提供。
- 每个用例都在临时工作区里做，并用 `sha256sum` 记录前后哈希作为证据。

### 成功条件（用户确认的边界）

- (a) 人手动（Web）回滚固定 `preserve`：只回滚 agent 的改动，不覆盖人的改动；字节混合可接受。
- (b) 普通步骤回滚和回滚失败都返回 201；失败时界面必须看得到文件处理结果的失败提示。
- (c) 人机都改过的文件：界面必须提示「此文件 agent 改过、人也改过，只回滚了 agent 的部分」；缺这条提示才算失败。当前文案「文件部分回退，部分后续值已保留」未写清，记为剩余缺口。
- (d) 附件：回滚后 Web 不再显示该附件即可；跨会话必须 404。

### Live status

- 2026-10-18 源码核对（未 live）：合并为单一 H1；修正分支前缀为 `history_spans` 引用、窗口事实名 `compact.window_rolled_over`、回滚先 `compact.retire`、删除旧 `compact.continued` 判据、附件 id 与 404/400 文案、`assistant.workspace_rescue` 为唯一结算事实、Web 已展示文件处理结果、冲突原因全表与 `deleted_paths`。
- 通过（r2，按新成功条件）：preserve 得到 `hOllo world` 且 `effect=partially_restored`（只回了 agent 部分、人的改动保留）；长度变化回滚失败仍 201，响应带 `workspace_file_result.effect=failed` 和中文 error；回滚后同会话附件仍 200 但 Web 不显示、跨会话 404。r2 早于 6c9f456，长度变化与附件可读性两项需在当前 main 复跑（当前源码下被回滚掉的附件在新会话应为 404）。
- **剩余缺口**：人机混合文件的界面提示仍是「文件部分回退，部分后续值已保留」，未写明「agent 改过、人也改过，只回滚了 agent 的部分」
- 未验：compact 进行中回滚（r2 跳过）
