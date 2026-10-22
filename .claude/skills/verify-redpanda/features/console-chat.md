# 终端入口

`console_chat.py` 是即开即用的终端单会话，使用自己的数据目录（默认 `~/.redpanda-tui`，`--home` 指定；不读 `REDPANDA_HOME` / `REDPANDA_CONFIG`），与 Web 的数据互不相干；同一目录同时只能开一个，会话不跨启动保留。来源：`console_chat.py`、`redpanda/channels/tui/console.py`、`images.py`。

## Sub-features

- `start`：每次启动都新建一个会话并打印：`RED PANDA 已启动。model=<模型>`、`工作区：<访问范围>`（完全访问时为 `工作区：整台电脑`，否则 `工作区：工作区 <工作区名>`）、`当前对话：<session_id>`，随后是提示行 `/new 新对话（关闭后不保留会话）`、`/mcp  /skill  管理外部能力`、`直接输入任务。运行中再输入会打断当前任务。`、`Ctrl+C 或 Ctrl+D 退出。`、`Ctrl+V 粘贴图片或文字；终端拦截时用 Alt+V。删除 [Image #n] 可取消附件。`。
- `new`：`/new` 另开新对话；切走后不能切回，没有 `/resume`。
- `first-run`：数据目录里没有可用默认模型时，启动前先在终端走模型配置向导。
- `fresh`：启动时丢弃上次运行留下的 `runtime_sessions/` 与 `state/`，保留 `config.json`、`connections.json` 与扩展登记；`--home` 已承载长期 Agent 工作树时拒绝启动并提示换独立目录，不清理该目录。
- `single-instance`：同一数据目录已有入口在运行时，第二个启动报错退出并写明占用者。
- `interrupt`：运行中再输入会打断当前任务。
- `approve`：待授权或待确认时输入 `yes`/`y` 批准、`no`/`n` 拒绝（见 extensions、tool-authorization）。
- `paste-image`：Ctrl+V（被拦截时 Alt+V）粘贴图片，删掉 `[Image #n]` 取消附件。
- `ext-commands`：`/mcp`、`/skill`（见 extensions）。
- `status-and-preview`：底栏显示当前 Session ID、最近输入用量、compact 阈值/次数/阶段与子 Agent 活动；普通回复支持流式预览，中止未完成的预览时显示 `[输出已中止]`。

## How to get to it (user POV)

- Linux：`./redpanda-env/bin/python console_chat.py --home "$RUN_DIR/tui-home" --workspace $SCRATCH_WORKSPACE`（缺省用启动目录作工作区）。
- Windows（PowerShell）：`.\redpanda-env\Scripts\python.exe console_chat.py --home <目录> --workspace <目录>`。
- 验证时必须传 `--home` 指向本次运行的目录，不要落到个人的 `~/.redpanda-tui`。

## Driving it with tmux

- Linux 用 `tmux new-session -d` 启动，`tmux send-keys` 输入，`tmux capture-pane -p` 取证。Windows 没有 tmux，需要真实控制台（Windows Terminal / ConPTY），由人操作或用能驱动 ConPTY 的工具，截屏或复制终端文本取证。
- **启动**：
  - (a) `workspaces.json` 登记了该目录；`runtime_sessions/` 有 `当前对话：` 打印的那个会话。
  - (b) 上面的启动信息逐行齐全；非完全访问时第二行为 `工作区：工作区 <name>`。
  - (c) 已配置模型时启动不改 `config.json`、`connections.json`；全新目录先进入模型配置向导。
  - (d) 再次启动后 `runtime_sessions/` 只剩新会话，上次的会话已清掉。
  - (e) 同一 `--home` 再开第二个 → 报错退出，信息含第一个的进程号。
- **/new**：(a) `runtime_sessions/` 多一个会话。(b) 打印 `新 Session 已创建：session-...`。(c) 原会话历史不变。
- **回复与底栏**：发送一条短任务，观察流式预览和完成正文；核对底栏 Session ID 与当前会话一致，模型返回用量后更新输入 token 数。`/new` 后底栏切到新 Session；停止未完成预览时记录 `[输出已中止]`，不能将预览记为最终回复。
- **与 Web 无关**：Web 与终端同时运行互不阻塞；终端建的会话不出现在 Web 的 Agent 名册（`GET /api/agents`）与收件箱。
- **退出**：Ctrl+D（或 Ctrl+C）退出，打印 `已退出。`。(c) 无残留子进程。

## Gotchas

- 需要真 TTY（prompt_toolkit）；不能用管道喂输入。Linux 用 tmux；Windows 必须在真实控制台（Windows Terminal / ConPTY）里跑。
- 与 Web 不同，终端缺省会用启动目录登记工作区，且每次启动都是全新会话，没有办法接回旧会话。
- 终端会话的文件工具与命令直接作用于工作区真实目录，没有回退；写文件与每条命令都要 yes/no 授权。委派的子 Agent 只读调查，没有写工具，也没有 `compare_subagent`/`merge_subagent`。
- 历史验证：2026-10-12（main@1a4790a）跳过，需真 TTY。2026-10-18 按源码改写启动文案与 Windows 入口，未 live 复跑。

- 2026-10-20 维护实测（`origin/main@32860f6`，Windows Chrome/ConPTY，隔离 home）：真实 Windows ConPTY 启动、/mcp list、/new、新会话 TUI_PING_OK 与 Ctrl+D 正常退出通过；证据为终端文本，无 Web 截图。底栏、粘贴、并发占用与重启清理未覆盖。

- 2026-10-22 全量维护（`origin/main@4c000ca`，Windows Chrome/ConPTY，隔离 home、真实 DeepSeek）：真实 Windows ConPTY 启动、/mcp list、/new、新会话 TUI_PING_OK 与 Ctrl+D 退出通过；更新终端说明后另建 home 复跑。粘贴、底栏和并发占用未重验。
