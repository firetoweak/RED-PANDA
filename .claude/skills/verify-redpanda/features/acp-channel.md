# ACP 入口

`acp_chat.py` 通过 Agent Client Protocol 让外部编辑器（如 Zed）驱动小熊猫。来源：`acp_chat.py`、`redpanda/channels/acp/agent.py`、`project.py`；协议实现来自 `acp` 库（`run_agent`、`PROTOCOL_VERSION`、`RequestError`）。

## Sub-features

- `initialize`：`protocolVersion` 必须等于 `acp` 库的 `PROTOCOL_VERSION`（当前 venv 中为 `1`）；响应 `agentInfo` 为 name `redpanda`、title `RED PANDA`、version `0.1.0`，`agentCapabilities.loadSession` 为 false，`promptCapabilities` 的 image/audio/embeddedContext 都是 false，`authMethods` 为空。
- `session-new`：每个会话的工作区来自 `session/new` 的 `cwd`（必须是已存在的绝对目录，会登记进 `workspaces.json`）；不支持 `mcpServers` 与 `additionalDirectories`。不支持 `session/load`。
- `prompt`：只接受文本块，多个文本块用换行拼接；回复以 `agent_message_chunk` 流式推送，结束时 `stopReason` 为 `end_turn`。
- `cancel`：`session/cancel` 让进行中的 `session/prompt` 以 `stopReason: "cancelled"` 返回。
- `approve`：待授权或待确认时，以 `yes`/`y`、`no`/`n` 作为下一条文本提示回答（见 extensions）。
- `--workspace`：只影响启动时 bootstrap 登记的工作区（缺省为启动目录），不决定各 ACP 会话的工作区。

## How to get to it (user POV)

- 在支持 ACP 的编辑器里把 agent 命令配置为 `<repo>/redpanda-env/bin/python acp_chat.py`（Windows 为 `<repo>\redpanda-env\Scripts\python.exe acp_chat.py`）；编辑器打开项目时发的 `cwd` 就是会话工作区。

## Driving it with an ACP client

- 用 ACP 客户端（或按协议手写 stdio JSON-RPC）发 `initialize` → `session/new`（`cwd` 为 `$SCRATCH_WORKSPACE` 的绝对路径）→ `session/prompt`。
- **握手与建会话**：
  - (a) `session/new` 返回的 `sessionId` 出现在 `$REDPANDA_HOME/runtime_sessions/`；`workspaces.json` 登记了 `cwd`。
  - (b) `initialize` 响应如上；`agentInfo.name` 为 `redpanda`。
  - (c) 两个会话用不同 `cwd` 时各自落在自己的工作区，互不影响。
  - (d) 以下都返回 JSON-RPC 错误且进程不退出：`protocolVersion` 不等于库版本 → invalid params（`protocolVersion: only 1 is supported`）；`cwd` 为相对路径或不存在 → invalid params（`cwd: must be an existing absolute directory`）；带非空 `mcpServers` 或 `additionalDirectories` → invalid params（`not supported`）。
- **提示**：
  - (a) journal 中有该文本的用户输入，之后有模型回复。
  - (b) 收到 `session/update` 流式更新，最后 `session/prompt` 返回 `stopReason: "end_turn"`。
  - (c) 一次提示只产生一个用户输入（不重复投递）。
  - (d) 含非文本块（如图片）→ invalid params（`prompt: only text is supported`）；未知 `sessionId` → `-32002 Session not found`；同一会话上一条提示未结束时再发 → `-32000 Session already has an active prompt`。
- **取消**：运行中发 `session/cancel` → (b) 该次 `session/prompt` 返回 `stopReason: "cancelled"`；(c) 取消后不再推送这轮的消息块。

## Gotchas

- 非法 JSON、未知方法等由 `acp` 库处理，不是本项目的判据。
- 风险未验证：stdout 必须只有协议消息，日志混入 stdout 会让客户端解析失败；源码没有专门守这一条，live 时要检查。
- 需要 ACP 客户端，本轮未 live 验证，记为 unverified-live。
- 历史验证：2026-10-12（main@1a4790a）跳过，需 ACP 客户端。2026-10-18 按源码改写，未 live 复跑。
