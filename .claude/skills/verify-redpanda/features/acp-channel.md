# ACP 入口

`acp_chat.py` 通过 Agent Client Protocol 让外部编辑器（如 Zed）驱动小熊猫。来源：`acp_chat.py`、`redpanda/channels/acp/agent.py`（agent 名 `redpanda`）、`project.py`。

## Sub-features

- `initialize`：ACP 握手返回 agent 信息 `redpanda`。
- `prompt`：会话中发提示并流式返回。
- `workspace`：`--workspace` 指定项目目录。

## How to get to it (user POV)

- 在支持 ACP 的编辑器里把 agent 命令配置为 `<repo>/redpanda-env/bin/python acp_chat.py --workspace <项目>`。

## Driving it with an ACP client

- 用 ACP 客户端（或按协议手写 stdio JSON-RPC）发 `initialize` → `session/new` → `session/prompt`。
- (a) `REDPANDA_HOME/runtime_sessions/` 出现会话。(b) `initialize` 响应含 `redpanda`；prompt 有流式更新与结束。(c) stdout 只输出协议消息，日志不得混入 stdout（否则客户端解析失败）。(d) 发送不合法 JSON → 返回协议错误，进程不退出。

## Gotchas

- 本轮未 live 验证；需要 ACP 客户端，记为 unverified-live。

## Live status
- 跳过（2026-10-12，main@1a4790a）：需 ACP 客户端
