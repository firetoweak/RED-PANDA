# 终端入口

`console_chat.py` 在终端里与 Assistant 对话。来源：`console_chat.py`、`redpanda/channels/tui/console.py`、`images.py`。

## Sub-features

- `start`：启动打印 `RED PANDA 已启动。model=...`、`工作区：...`（完全访问时为 `整台电脑`）、`当前对话：<id>`。
- `new` / `resume`：`/new` 新对话，`/resume <id>` 恢复。
- `interrupt`：运行中再输入会打断当前任务。
- `paste-image`：Ctrl+V（被拦截时 Alt+V）粘贴图片，删掉 `[Image #n]` 取消附件。
- `ext-commands`：`/mcp`、`/skill`（见 extensions）。

## How to get to it (user POV)

- `REDPANDA_HOME=... ./redpanda-env/bin/python console_chat.py --workspace $SCRATCH_WORKSPACE`（缺省用启动目录作工作区）。

## Driving it with tmux

- 用 `tmux new-session -d` 启动，`tmux send-keys` 输入，`tmux capture-pane -p` 取证。
- **启动**：(b) 三行启动信息齐全。(a) `workspaces.json` 登记了该目录。
- **/new**：(b) 打印 `新 Session 已创建：session-...`。(a) `runtime_sessions/` 多一个目录。
- **/resume**：(d) `/resume` 无参数 → `用法：/resume <session_id>`；不存在的 id → `Session 不存在：...`，当前会话不变。合法 id → `已恢复 Session：...`。
- **跨入口一致**：在终端建的会话，在 Web（同一 home）的 `GET /api/sessions` 中可见且历史一致。
- **退出**：Ctrl+D 退出，(c) 无残留子进程、FUSE 挂载已卸载（`mount | grep redpanda` 为空）。

## Gotchas

- 需要真 TTY（prompt_toolkit）；不能用管道喂输入。
- 与 Web 不同，终端缺省会用启动目录登记工作区。
