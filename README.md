# konden

<p align="center">
  <img src="assets/brand/konden-social-preview-v1.png" width="1280" alt="konden — 你的 AI 团队，持续协作。独立验证、持续跟进、随时介入。">
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#codex-接入mcp">接入 Codex</a> ·
  <a href="docs/README.md">文档</a>
</p>

**一支长期协作的 AI 团队，接上你正在进行的工作。**

konden 提供一支有名字、能互相交流的 Bot 团队，承担独立验证与持续跟进，减少你重复说明背景、转交报告和推动下一步的工作。

每位 Bot 都有自己的持久工作目录，通过私信和群聊协作。你设定目标、决定授权，也可以随时加入讨论、调整方向。


## 快速开始

支持 **Windows x64**、**Linux x64（glibc 2.35+）** 与 **macOS 15+（Apple Silicon / Intel）（注意，本项目并未在MAC系统上实机使用过！！！）**。

macOS 需要可用的 Git；若系统尚未准备命令行工具，先运行 `xcode-select --install` 并完成系统安装。

```sh
git clone https://github.com/firetoweak/konden.git konden
cd konden
```

先运行准备脚本，选择与你的系统对应的命令：

**Windows（PowerShell）**

```powershell
.\scripts\setup.ps1
```

**Linux / macOS（终端）**

```sh
sh scripts/setup.sh
```

准备脚本会补齐 Python、Node.js 等运行工具，不修改系统 PATH；参数说明见[准备选项](docs/运行指南.md#准备选项)。

setup 完成后，先按[个人连接文件说明](docs/模型配置.md#个人连接文件)填写模型密钥。打开终端打印的「模型连接配置」文件（默认 Windows 为 `%USERPROFILE%\.konden\connections.json`，Linux/macOS 为 `~/.konden/connections.json`），在所用供应商项的 `api_key` 中填入自己的 API Key 并保存，保留其他供应商项。本地服务地址也在这个文件中配置；Codex 登录方式见[复用 Codex 登录额度](docs/模型配置.md#复用-codex-登录额度)。

填好后再启动：

**Windows（PowerShell）**

```powershell
.\scripts\start.ps1
```

**Linux / macOS（终端）**

```sh
sh scripts/start.sh
```

需要指定监听地址或端口时，在启动命令后追加 `--host 0.0.0.0 --port 8765`，用法与访问地址见[启动参数](docs/运行指南.md#启动参数)。

启动后打开终端打印的地址，在侧栏「设置 → 模型配置」中[添加相应模型](docs/模型配置.md#web-配置与会话切换)，保存后点击「测试」确认连接，再新建 Bot、说明你希望它承担的工作。需要单独修改代码时，konden为每个bot提供单独的worktree，可直接关联项目。见[关联已有首个提交的 Git 仓库](docs/运行指南.md#模型与工作区)。

> !Mac 未实际实机使用！

Bot 可按需使用随环境安装的 Chrome DevTools MCP、Playwright 和应用专用 Chrome，进行浏览器操作与真实流程验证。搜索优先使用你已启用的搜索 MCP，其次使用已配置密钥的内置 DeepSeek；配置、数据位置与其他入口见[运行指南](docs/运行指南.md)。

## Codex 接入（MCP）

1. 打开「设置」→「Codex 接入」，生成 MCP 连接配置，配置到 Codex 中。
2. Codex 聊天领取自己的会话身份后，即可在 Web 侧栏「外部会话」看到它；每个新聊天领取独立身份，原聊天重连时沿用。
3. 联系长期 Bot 讨论需求、开展调查或交接工作，持续读取它们的答复与后续消息。普通交流无需代码项目或版本。

可以这样向 Codex 说明任务：

> 请通过已连接的 konden 联系xx，先讨论这个需求，保留当前聊天的身份并持续收取回复。

连接配置可复用于多个 Codex 聊天，各聊天分别领取身份。共用凭据下的身份用于分流，不提供权限隔离；外部消息也不能代你批准命令或读取无关私聊。配置位置、工具调用、自动接续与撤销方式见[Codex 会话接入指南](docs/外部协作.md)。

## 开发与文档

开发启动使用 `scripts/dev.ps1` / `scripts/dev.sh`。本地检查使用 `python scripts/ci.py`，验证选项见[统一验证入口](scripts/ci.py)，测试分层见[架构总览](docs/架构/总览.md#测试分层)。

| 想了解什么 | 从这里开始 |
| --- | --- |
| 产品解决的问题、当前场景与边界 | [产品定位](docs/产品定位.md) |
| 安装参数、运行环境与数据位置 | [运行指南](docs/运行指南.md) |
| Codex 接入长期 Bot 对话 | [Codex 会话接入指南](docs/外部协作.md) |
| 在外部对话中交付独立验证 | [独立验证](docs/独立验证.md) |
| 模型连接与切换 | [模型配置](docs/模型配置.md) |
| 源码验证入口 | [统一验证脚本](scripts/ci.py) |
| 设计取舍与演进 | [架构方向](docs/项目架构方向.md) · [架构总览](docs/架构/总览.md) |
| 全部文档 | [文档索引](docs/README.md) |

## 许可证

本项目采用 [MIT License](LICENSE)。
