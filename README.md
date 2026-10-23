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

安装参数、平台要求、终端入口与数据位置见[运行指南](docs/运行指南.md)。

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
