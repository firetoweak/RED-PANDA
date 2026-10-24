<p align="center">
  <img src="assets/brand/red-panda-full.png" width="240" alt="RED PANDA 折纸小熊猫">
</p>

<h1 align="center">RED PANDA</h1>

<p align="center">让你正在使用的原生编码工具，与一支长期 Bot 团队协作。</p>

> [!WARNING]
> 项目持续演进，**近期会频繁更新**。功能、界面和本地数据格式都可能发生不兼容的变化，不保留旧版兼容。产品定位见 [PRODUCT.md](PRODUCT.md)，演进方向见 [Coding Bot](docs/架构/CodingBot/方向.md)。

你继续在 Codex、Claude Code、Cursor 等原生工具里开发，RED PANDA 提供一支可以通过 MCP 联系的长期 Bot 团队，减少你重复说明背景、发起验证、转交反馈和协调成员的工作。当前聚焦独立验证：把需求与明确版本交给团队，从真实用户操作和边界条件出发取证，将可行动的反馈交回原编码会话，修复后继续复验。原生客户端需要主动等待和处理反馈，具体用法与边界见下方[独立验证接入](#独立验证接入mcp)。

Bot 以名字维持身份，各有持久工作目录，通过私信与群聊讨论、分工和跟进。人负责目标、授权与最终取舍；代码项目按需关联，分析资料与讨论想法同样可以独立开展。用户保留原生开发体验，RED PANDA 不接管外部编码工具的会话与执行生命周期。核心用户价值与产品边界见 [PRODUCT.md](PRODUCT.md)。

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

准备脚本会补齐运行所需的 Python、Node.js 等工具，不修改系统 PATH，也不需要预装 Python、Rust 或 Docker。启动后打开终端打印的地址，在侧栏「模型配置」中添加模型，再新建 Bot 开始使用；需要在代码项目中执行任务时，再关联已有首个提交的 Git 仓库。

每个 Bot 默认可按需加载 Chrome DevTools MCP 与 Playwright，进行网页调试、浏览器操作和真实流程验证。准备脚本会安装这两项能力及应用专用的 Chrome；更新已有环境后重新运行准备脚本即可。各连接使用独立的无头浏览器，关闭后不保留登录状态。

网上检索默认优先使用用户已配置并启用的搜索 MCP（如 Tavily），没有或不可用时再用内置 Exa，最后使用浏览器兜底。Exa 搜索与正文读取无需 API Key，免费服务有限流。检索 Skill 提供来源核实与浏览器兜底指引。准备脚本会发送公开查询检查备用 Exa 的搜索和网页读取；可用 `python -m redpanda.mcp.check_web` 随时复查，本次检查不代表长期可用保证。

安装参数、平台要求、终端入口与数据位置见[运行指南](docs/运行指南.md)。

## 独立验证接入（MCP）

启动 Web，从「设置」→「外部验证接入」生成并复制连接配置，添加到你使用的编码工具。无需给外部智能体取名或选择固定交接人。凭据只在生成时显示，关闭设置前请保存；遗失后撤销并重新生成。目前用于本机连接。

创建负责验证的 Bot 并说明分工。编码端首次调用 `get_context` 自动领取当前聊天的 `identity`、查看同事分工，随后用 `request_verification` 交付验收需求、`reference`（PR 或可获取该版本的仓库/成果引用）、`revision`（完整提交号），并按分工选择 `recipient`。同一配置可以供多个聊天复用，各聊天分别领取身份；重连沿用原身份。

原编码会话接着用 `read_feedback` 等待并按 `next_cursor` 续读。验证 Bot 从真实用户行为与边界独立取证，用 `reply_external_message` 区分进度、问题和最终报告。报告给出通过、发现问题或受阻，实际测试版本与证据；自测或 CI 绿灯不能替代验证。编码端收到问题后修复，以新版本、新 `delivery_id` 和 `previous_request_id` 请求复验；复验省略收件人时沿用原同事。网络重试保持原身份、提交标识与内容。

接入允许团队在已有协作范围内交换验证任务与结果，外部请求不能代你批准命令或读取无关私聊。共用凭据下的聊天身份用于分流，不是权限隔离。全局设置可撤销该连接配置，所有使用它的聊天立即失去收发能力，既有验证记录继续显示在 Bot 时间线。客户端需在当前会话主动等待和处理反馈；本入口不负责在客户端结束后自动唤醒它。

## 开发

- 热重载启动：`scripts/dev.ps1` / `scripts/dev.sh`
- 测试：`python -m pytest`；前端 `npm --prefix web test`
- 与 CI 相同的本地验证：`python scripts/ci.py`

详见[开发指南](docs/开发指南.md)。

## 文档

| 想了解什么 | 从这里开始 |
| --- | --- |
| 产品解决什么问题、当前聚焦场景与边界 | [产品定位](PRODUCT.md) |
| 安装参数、其他入口与数据位置 | [运行指南](docs/运行指南.md) |
| 开发与测试 | [开发指南](docs/开发指南.md) |
| 模型连接与切换 | [模型配置](docs/模型配置.md) |
| 设计与架构 | [架构方向](docs/项目架构方向.md) · [架构总览](docs/架构/总览.md) |
| 全部文档 | [文档索引](docs/README.md) |

## 许可证

当前仓库尚未添加 `LICENSE`，许可证待确定。
