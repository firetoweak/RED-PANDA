# 改写消息与分支

改写一条已发出的用户消息并重新执行；或从某一轮之后建立新会话。来源：`EditableUserMessage.tsx`、`Conversation.tsx`、`app.py` 的 `POST .../forks`（`message_id`、`text`、`listed`、`restore_files`）与 `POST .../branches`、`redpanda/assistant/host/session_store.py`、`supervisor.py` 的 `branch_after_turn`。

## Sub-features

- `edit-rerun`：用户消息上 `编辑消息`（提示 `编辑或原文重发`）→ 改文字 → `改写并执行`；改写涉及文件时对话框给出 `只回滚会话` / `会话和文件一起回滚`。
- `copy-message`：`复制消息` → `已复制消息`。
- `branch`：助手轮次上 `从这一轮之后建立新会话` → 新会话。

## How to get to it (user POV)

- 悬停任一用户消息出现编辑/复制按钮；悬停轮次末尾出现 `从这一轮之后建立新会话`。

## Driving it with the browser

- **改写（不动文件）**：把首条消息改为 `请只回复 EDITED_OK` → `改写并执行` → `只回滚会话`。
  - (a) `GET /api/sessions/<新或原 id>`：改写点之后的旧回合消失，新回复含 `EDITED_OK`；工作区文件不变。
  - (b) `POST .../forks` 201。
  - (c) 改写点之前的消息原样保留；原会话是否保留在列表里取决于 `listed`，记录实际行为。
  - (d) 用不存在的 `message_id` 调 forks → 404（`ForkMessageNotFoundError`）；运行中的会话调 forks → 409（`SessionForkUnavailableError`），会话不变。界面上连接未建立时按钮提示 `连接建立后可编辑`。
- **改写 + 回滚文件**：在 tool-coding-round 之后改写那条编码请求并选 `会话和文件一起回滚`。(a) `note.txt` 回到该消息发出前（`verify`），之后按新请求再变化。
- **分支**：在第一轮之后 `从这一轮之后建立新会话`。(a) `GET /api/sessions` 多一个会话，其历史只到所选轮次；原会话完整不变。(b) `POST .../branches` 201，地址跳到新会话。(c) 工作区文件不变（分支不复制/回滚文件）。(d) 失败时界面提示 `未能从这一轮建立新会话。`。

## Gotchas

- 改写失败提示为 `消息编辑失败，未能改写这条消息。`。
- 改写会丢弃之后的回合，验证前确认不是 file-rewind 要用的会话。
