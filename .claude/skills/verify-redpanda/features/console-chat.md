# 终端入口

`console_chat.py` 在终端里与 Assistant 对话。来源：`console_chat.py`、`redpanda/channels/tui/console.py`、`images.py`。

## Sub-features

- `start`：每次启动都新建一个会话并打印：`RED PANDA 已启动。model=<模型>`、`工作区：<访问范围>`（完全访问时为 `工作区：整台电脑`，否则 `工作区：工作区 <工作区名>`）、`当前对话：<session_id>`，随后是提示行 `/new 新对话    /resume <id> 恢复`、`/mcp  /skill  管理外部能力`、`直接输入任务。运行中再输入会打断当前任务。`、`Ctrl+C 或 Ctrl+D 退出。`、`Ctrl+V 粘贴图片或文字；终端拦截时用 Alt+V。删除 [Image #n] 可取消附件。`。
- `new` / `resume`：`/new` 新对话，`/resume <id>` 恢复。
- `interrupt`：运行中再输入会打断当前任务。
- `approve`：待授权或待确认时输入 `yes`/`y` 批准、`no`/`n` 拒绝（见 extensions、tool-authorization）。
- `paste-image`：Ctrl+V（被拦截时 Alt+V）粘贴图片，删掉 `[Image #n]` 取消附件。
- `ext-commands`：`/mcp`、`/skill`（见 extensions）。

## How to get to it (user POV)

- Linux：`REDPANDA_HOME=... ./redpanda-env/bin/python console_chat.py --workspace $SCRATCH_WORKSPACE`（缺省用启动目录作工作区）。
- Windows（PowerShell）：`$env:REDPANDA_HOME = '...'` 后 `.\redpanda-env\Scripts\python.exe console_chat.py --workspace <目录>`。

## Driving it with tmux

- Linux 用 `tmux new-session -d` 启动，`tmux send-keys` 输入，`tmux capture-pane -p` 取证。Windows 没有 tmux，需要真实控制台（Windows Terminal / ConPTY），由人操作或用能驱动 ConPTY 的工具，截屏或复制终端文本取证。
- **启动**：
  - (a) `workspaces.json` 登记了该目录；`runtime_sessions/` 有 `当前对话：` 打印的那个会话。
  - (b) 上面的启动信息逐行齐全；非完全访问时第二行为 `工作区：工作区 <name>`。
  - (c) 启动不改 `config.json`、`connections.json`。
  - (d) 未配置默认模型时发消息 → 打印 `模型配置：<原因>`，进程不退出。
- **/new**：(a) `runtime_sessions/` 多一个会话。(b) 打印 `新 Session 已创建：session-...`。(c) 原会话历史不变。
- **/resume**：
  - (b) 合法 id → `已恢复 Session：<id>`，随后打印运行状态。
  - (c) 失败时当前会话不变（之后发的消息仍进原会话）。
  - (d) `/resume` 无参数 → `用法：/resume <session_id>`；不存在的 id → `Session 不存在：<id>`；会话的工具集加载失败 → `Session 恢复失败：<code>: <message>`。
- **跨入口一致**：在终端建的会话，在 Web（同一 home）的 `GET /api/sessions` 中可见且历史一致。
- **退出**：Ctrl+D（或 Ctrl+C）退出，打印 `已退出。`。(c) 无残留子进程；Linux 上 FUSE 挂载已卸载（`mount | grep redpanda` 为空）。

## Gotchas

- 需要真 TTY（prompt_toolkit）；不能用管道喂输入。Linux 用 tmux；Windows 必须在真实控制台（Windows Terminal / ConPTY）里跑。
- 与 Web 不同，终端缺省会用启动目录登记工作区，且每次启动都新建会话；要接着旧会话需 `/resume <id>`。
- 历史验证：2026-10-12（main@1a4790a）跳过，需真 TTY。2026-10-18 按源码改写启动文案与 Windows 入口，未 live 复跑。
