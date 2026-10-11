# Web session and chat

From an empty personal directory, a person creates a workspace by typing a folder path, and the home page opens a draft session. Sending a message needs a configured model; the composer stays on screen either way.

## Sub-features

- `workspace-create` creates a named workspace from the empty home.
- `session-open` shows the draft composer for that workspace.
- `composer-ready` shows `已连接` and the message box.
- `chat-send` sends one message and shows it in the transcript. Skip when Doctor reports `live_model_ready` false.

## How to get to it (user POV)

- Open the Web origin.
- Choose `创建工作区` on the empty home.
- Type the workspace name and an existing folder path, then choose `创建`.
- After that first workspace exists, the page opens the draft itself. Later sessions start from `新建会话` or from `在 <name> 新建会话`.
- Type in `消息` and choose `发送`, or press Enter.

## Driving it with the browser

Preconditions:

- Doctor exits 0 and `workspace_count` is 0.
- `$SCRATCH_WORKSPACE` is an existing directory outside `$REDPANDA_HOME`.
- Viewport is at least 1280×800.

- **Open the empty home.** Run `browser open $REDPANDA_VERIFY_URL/`. The heading reads `今天想一起完成什么？` and the sidebar text is `已连接`.
- **Open the create dialog.** Choose `创建工作区`. Run `browser click --role button --name "创建工作区"`. A dialog titled `新建工作区` is visible, with textboxes `名称` and `目录`.
- **Name the workspace.** Run `browser fill --role textbox --name "名称" --value "verify-workspace"` and `browser fill --role textbox --name "目录" --value "$SCRATCH_WORKSPACE"`. Do not choose `选择文件夹`.
- **Create it.** Run `browser click --role button --name "创建"`. The dialog closes. A button named `在 verify-workspace 新建会话` is in the sidebar, and the message box `消息` is on the page. The URL path starts with `/sessions/`.
- **Confirm the workspace.** Run `curl -fsS $REDPANDA_VERIFY_URL/api/workspaces`. The list contains one object whose `name` is `verify-workspace` and whose `task_root` is the resolved scratch path. `workspace_id` starts with `workspace-`.
- **Send, only with a model.** If `live_model_ready` is false, stop this feature here and write `skipped chat-send` in the note. Otherwise fill `消息` with `请只回复 VERIFY_CHAT_OK，不要调用工具。` and run `browser click --role button --name "发送"`. The transcript shows that sentence. `发送` is disabled until the reply settles or an error is shown.
- **Confirm the transcript.** Copy the session id from the URL. Run `curl -fsS $REDPANDA_VERIFY_URL/api/sessions/<session id>`. `items` contains one object with `"kind": "user"` and that same text. A later assistant step is the reply, not a substitute for the user item.
- **Proof.** Run `browser snapshot --aria --path $EVIDENCE_DIR/web-session-chat/snapshot.txt` and `browser screenshot --path $EVIDENCE_DIR/web-session-chat/page.png`. Save the workspaces or conversation JSON as `state.json`.

## Gotchas

- `新建工作区` matches both the sidebar icon and the empty-state button. Use `创建工作区` on the empty home.
- `选择文件夹` opens a desktop picker and returns 503 when no display is available. Type the path.
- The folder must already exist. A second workspace on the same path shows `创建失败`.
- Creating the first workspace while the URL is `/` opens a draft session without another click. The sidebar title for that draft is `new agent` until the first user message.
- `新建会话` stays disabled while the sidebar says `正在连接…` or there is no workspace.
- `发送` stays disabled until the session model's provider is configured. That disabled button is the skip signal for `chat-send`.
- Enter sends. Shift+Enter inserts a newline. Enter during IME composition does not send.
- Posting `/api/sessions/<id>/inputs` does not prove this feature.
