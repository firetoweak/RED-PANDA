# 改写消息与分支

改写一条已发出的用户消息并重新执行（产生新会话、取代原会话）；或从某一轮之后建立一条并列的新会话。来源：`EditableUserMessage.tsx`、`Conversation.tsx`、`AttachmentTile.tsx`、`FileAttachmentTile.tsx`、`app.py` 的 `POST .../forks`（`EditRequest`：`message_id`、`text`、`artifact_refs`、`listed`、`restore_files`）与 `POST .../branches`（`BranchRequest`：`message_id`）、`redpanda/channels/web/channel.py`（`_require_refs`）、`redpanda/assistant/host/session_store.py`（`fork_before_message`、`fork_after_turn`）、`supervisor.py` 的 `fork_and_accept_input` 与 `branch_after_turn`。

## Sub-features

- `edit-rerun`：用户消息上 `编辑消息`（提示 `编辑或原文重发`；连接未建立时提示 `连接建立后可编辑` 且禁用）→ 进入编辑态（文本框 aria-label `编辑消息`）→ 改文字 → `改写并执行`（Enter 也提交，Shift+Enter 换行）。
- `edit-confirm`：当这条消息之后还有工作（`hasLaterWork`：不是最后一轮，或这一轮有执行步骤）时，提交前弹出 `改写这条消息` 对话框，正文「对话会从这条消息重新开始。文件可以一起回滚到这条消息之前，也可以保持现状。」，按钮 `只回滚会话`（`restore_files=false`）/ `会话和文件一起回滚`（`restore_files=true`）。否则不弹框，直接以 `restore_files=false` 提交。
- `edit-cancel`：`取消编辑` 按钮、Esc、或在编辑区外按下指针都会取消编辑并恢复原文（保存中或确认框打开时不取消）。
- `edit-attachments`：编辑态下附件可移除：文件附件按钮 `Remove <文件名>`，图片附件 `Remove 图片`；提交的 `artifact_refs` 为保留下来的附件 id。
- `edit-fork-semantics`：Web 固定 `listed:false`，新会话取代原会话，原会话从 `GET /api/sessions` 列表隐藏。服务端不检查原会话是否在运行：运行中改写通常仍 201，原会话被标记为取代但不会被取消。
- `copy-message`：`复制消息` → 按钮 aria-label 变为 `已复制消息`（tooltip `已复制`），1.5 秒后恢复。
- `branch`：轮次末尾 `从这一轮之后建立新会话` → 新会话，历史含这一轮全部回复、不含之后的用户消息；原会话不被取代，仍在列表里；不处理文件（不写文件结算事实）；不写新的用户消息。

## How to get to it (user POV)

- 悬停任一用户消息出现 `编辑消息` / `复制消息`；悬停轮次末尾出现 `从这一轮之后建立新会话`。

## Driving it with the browser

- **改写（不动文件）**：把首条消息改为 `请只回复 EDITED_OK` → `改写并执行` →（之后还有回合时出现 `改写这条消息`）`只回滚会话`。
  - (a) `GET /api/sessions/<新 id>`：改写点之后的旧回合消失，新回复含 `EDITED_OK`；工作区文件不变。`GET /api/sessions` 不再列出原会话。
  - (b) `POST .../forks` 201，body `session_id` 为新会话，地址以 replace 方式跳过去。
  - (c) 改写点之前的消息原样保留；原会话 `GET /api/sessions/<原 id>` 仍可读，历史不变。
  - (d) 用不存在的 `message_id` 调 forks → 404（`ForkMessageNotFoundError`）；`message_id` 指向非用户消息的事件 → 409（`fork target must be a user message`）；改写点之前的前缀不在外部事实边界 → 409（`fork prefix must end at an external-fact boundary`）。请求体缺 `artifact_refs` → 422（该字段在 `EditRequest` 中必填）；`artifact_refs` 含不属于当前会话的附件 → 400（`附件不属于当前会话`）。界面上失败时出现 `操作失败`，文字取服务端 `detail`，缺省为 `消息编辑失败，未能改写这条消息。`。
- **最后一轮无步骤的改写**：最后一条用户消息且该轮没有执行步骤时，`改写并执行` 不弹 `改写这条消息`，请求 `restore_files=false`。
- **运行中改写**：模型还在执行时改写较早的消息。(a) 通常 201，生成新会话；原会话被取代、从列表隐藏，但其运行没有被取消（`GET /api/sessions/<原 id>` 观察其状态）。(c) 记录原会话后续写入是否影响共享工作区。这是当前源码行为，记录为观察项。
- **改写 + 回滚文件**：在 tool-coding-round 之后改写那条编码请求并选 `会话和文件一起回滚`。(a) `note.txt` 回到该消息发出前（`verify`），之后按新请求再变化；新会话 `workspace_file_result.policy=preserve`。
- **取消编辑**：进入编辑态改几个字，分别按 `取消编辑`、Esc、点击编辑区外。(a) 消息显示原文，没有发出 forks 请求。
- **改写时移除附件**：见 [rewind-edge-cases.md](./rewind-edge-cases.md) 的 `edit-with-attachment`；`Remove <文件名>` 后提交，新会话该消息不带该附件。
- **分支**：在第一轮之后 `从这一轮之后建立新会话`。(a) `GET /api/sessions` 多一个会话，其历史只到所选轮次；原会话仍在列表且完整不变。(b) `POST .../branches` 201，地址跳到新会话。(c) 工作区文件不变（分支不复制/回滚文件，新会话没有 `workspace_file_result`）。(d) `message_id` 不存在 → 404；所选轮次尚未收口（仍在运行或等待授权）→ 409（`turn must end at an external-fact boundary`）；界面失败提示缺省文字 `未能从这一轮建立新会话。`。
- **复制**：点 `复制消息` → 剪贴板为消息原文，按钮名变 `已复制消息`。

## Gotchas

- 是否弹 `改写这条消息` 只看之后是否还有工作，与是否改过文件无关。
- 改写会丢弃之后的回合并让原会话从列表消失，验证前确认不是 file-rewind 要用的会话。
- 运行中改写不会被 409 挡住，也不会停止原会话；不要按「运行中调 forks → 409」判。
- 编辑态外点击会直接取消编辑，自动化点击其他控件前先提交或确认。

### Live status

- 2026-10-18 源码核对（未 live）：修正确认框触发条件（`hasLaterWork`）、运行中改写不返回 409、409 的真实来源、`artifact_refs` 必填与 400 文案、分支错误码与不取代原会话；补充取消编辑与附件移除控件。
- 通过（2026-10-12，main@1a4790a）：改写后模型回复偏题，待查
- 已知产品缺口（2026-10-18 Windows live）：在「从这一步之后重开」得到的会话里，改写重开后发出的第一条用户消息 → `POST .../forks` 409，界面 `操作失败。fork prefix must end at an external-fact boundary`。前缀停在被保留的步骤而非外部事实边界；`编辑消息` 按钮照常提供，且向用户显示内部英文错误。
