<p align="center">
  <img src="assets/brand/red-panda-full.png" width="240" alt="RED PANDA 折纸小熊猫">
</p>

<h1 align="center">RED PANDA</h1>

<p align="center">面向开发者的 coding bot：几个长期在线的 Agent 在同一项目上协作。</p>

> [!WARNING]
> 项目正在从单会话 coding agent 转向多个长期在线 Agent 协作的 coding bot，**近期会频繁更新**。功能、界面和本地数据格式都可能发生不兼容的变化，不保留旧版兼容。方向见 [Coding Bot](docs/架构/CodingBot/方向.md)。

RED PANDA 连接你选择的模型，让几个长期在线的 Agent 在同一个项目里理解代码、修改文件、执行命令并验证结果，彼此私信和群聊协作。行动过程可以查看，执行命令需要授权；每个 Agent 在自己的 Git 分支上工作，改动可以按差异审阅，合入由你决定。

## 快速开始

支持 Windows x64 与 Linux x64（glibc 2.35+），暂不支持 macOS。

```sh
git clone https://github.com/firetoweak/RED-PANDA.git
cd RED-PANDA
```

准备运行环境，再启动：

| | Windows（PowerShell） | Linux |
| --- | --- | --- |
| 准备 | `.\scripts\setup.ps1` | `sh scripts/setup.sh` |
| 启动 | `.\scripts\start.ps1` | `sh scripts/start.sh` |

准备脚本会补齐运行所需的 Python、Node.js 等工具，不修改系统 PATH，也不需要预装 Python、Rust 或 Docker。启动后打开终端打印的地址，在侧栏「模型配置」中添加模型，新建工作区（须是已有首个提交的 Git 仓库），再在名册里新建 Agent 开始使用。

每个 Bot 默认可按需加载 Chrome DevTools MCP 与 Playwright，进行网页调试、浏览器操作和真实流程验证。准备脚本会安装这两项能力及应用专用的 Chrome；更新已有环境后重新运行准备脚本即可。各连接使用独立的无头浏览器，关闭后不保留登录状态。

网上检索默认优先使用用户已配置并启用的搜索 MCP（如 Tavily），没有或不可用时再用内置 Exa，最后使用浏览器兜底。Exa 搜索与正文读取无需 API Key，免费服务有限流。检索 Skill 提供来源核实与浏览器兜底指引。准备脚本会发送公开查询检查备用 Exa 的搜索和网页读取；可用 `python -m redpanda.mcp.check_web` 随时复查，本次检查不代表长期可用保证。

安装参数、平台要求、终端入口与数据位置见[运行指南](docs/运行指南.md)。

## 外部编码智能体接入（MCP）

启动 Web 并创建一位 Bot，打开聊天页的「显示 Bot 资料」→「管理外部接入」，填写接入名称并创建，随后「复制配置」。将其中的 URL 与 Authorization 请求头填入外部编码客户端的 MCP 配置。凭据只允许联系这位 Bot，且只在创建后显示；关闭窗口前请保存，遗失后撤销并重新创建。当前 MCP 入口用于本机连接。

建立接入表示你允许 Bot 在已有的协作范围内与这位外部编码端交换任务和结果，普通任务答复无需逐条确认；你之后提出的交流限制仍然优先。外部请求不能代你批准命令、披露私聊或授予其他操作权限。

一份接入配置可以供多个聊天复用。外部端首次调用 `get_context` 领取当前聊天的 `identity`，后续 `send_message` 与 `read_messages` 必须带它；同一聊天与断线重连沿用原值，新聊天重新领取。消息和 `next_cursor` 按接入与身份分别组织；共用凭据下的身份用于分流交流，不是权限隔离。

外部端调用 `send_message` 发送请求，再用 `read_messages` 读取反馈并为当前身份保存 `next_cursor`。测试请求请提供 `reference`（PR 或成果引用）与 `revision`（完整提交号）。同一消息网络重试保持原 `identity`、`delivery_id` 和内容，新请求使用新消息标识。Bot 使用 `reply_external_message` 显式回复外部端，回复自动回到原请求身份；普通回答仍给人看。反馈中的 `revision` 保留请求版本，实际测试版本与结论由 Bot 在正文说明。

撤销时在同一「管理外部接入」窗口找到名称并确认「撤销」。这份接入下的所有聊天立即失去收发能力，历史仍在 Bot 的时间线中，标明外部名称与会话身份。外部端自行决定何时再次读取；此入口不负责在编码客户端空闲时启动或恢复它。

## 开发

- 热重载启动：`scripts/dev.ps1` / `scripts/dev.sh`
- 测试：`python -m pytest`；前端 `npm --prefix web test`
- 与 CI 相同的本地验证：`python scripts/ci.py`

详见[开发指南](docs/开发指南.md)。

## 文档

| 想了解什么 | 从这里开始 |
| --- | --- |
| 安装参数、其他入口与数据位置 | [运行指南](docs/运行指南.md) |
| 开发与测试 | [开发指南](docs/开发指南.md) |
| 模型连接与切换 | [模型配置](docs/模型配置.md) |
| 设计与架构 | [架构方向](docs/项目架构方向.md) · [架构总览](docs/架构/总览.md) |
| 全部文档 | [文档索引](docs/README.md) |

## 许可证

当前仓库尚未添加 `LICENSE`，许可证待确定。
