# helperMe

面向个人使用的 AI 助手。

## 快速开始

准备 ripgrep，以及 [DeepSeek API Key](https://platform.deepseek.com/api_keys)。不需要预装 Python 或 Docker。

### 1. 安装

在仓库目录运行对应平台脚本。脚本会下载项目专用 Python、安装依赖和 Ferro，并在 `.env` 中写入 `FERRO_MASTER_KEY`。它不会询问供应商密钥。

Windows（PowerShell）：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

macOS / Linux：

```sh
sh scripts/setup.sh
```

### 2. 把供应商密钥写入 .env

安装完成后，打开项目根目录的 `.env`，补上这一行再继续：

```text
DEEPSEEK_API_KEY=你的密钥
```

当前 `ferro/config.yaml` 的 target 是 `deepseek`。Ferro 只在启动时读取这个变量；漏掉它就没有供应商，模型请求会失败。控制台页面不能事后补填。

### 3. 启动

先在一个终端启动 Ferro 并保持运行，再在另一个终端启动 HelperMe。HelperMe 会从 `.env` 读取 `FERRO_MASTER_KEY` 调用 Ferro，不必打开控制台。http://localhost:18787/login 是 Ferro 自己的运维页面；命令打印的 `Gateway key` 只在要登录该页面时使用。

Windows（PowerShell）：

```powershell
.\helperme-env\Scripts\python.exe -m helperme.ferro_gateway serve
# 另开终端
.\helperme-env\Scripts\python.exe console_chat.py
```

macOS / Linux：

```sh
./helperme-env/bin/python -m helperme.ferro_gateway serve
# 另开终端
./helperme-env/bin/python console_chat.py
```

首次启动会创建个人配置，默认使用 `deepseek-v4-pro`。模型切换方法见[模型配置指南](docs/模型配置.md)。Python 环境位于项目目录的 `helperme-env`，可直接删除此目录清理环境；Agent 命令中的 Python 也使用该环境。个人配置和会话保存在 `~/.helperme`。

## 文档

[文档索引](docs/README.md) · [架构总览](docs/架构/总览.md) · [自举开发](docs/自举开发.md)
