# helperMe

面向个人使用的 AI 助手。

## 快速开始

准备 ripgrep，以及一个模型来源：[DeepSeek API Key](https://platform.deepseek.com/api_keys)、阿里云百炼 Qwen API Key，或本地运行的 vLLM。不需要预装 Python 或 Docker。

### 1. 安装

在仓库目录运行对应平台脚本。脚本会下载项目专用 Python 并安装依赖。它不会询问模型密钥。

Windows（PowerShell）：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

macOS / Linux：

```sh
sh scripts/setup.sh
```

### 2. 写入模型密钥

把项目根目录的 [.env.example](.env.example) 复制为 `.env`，填上所用模型来源的设置。默认模型是 DeepSeek，只需要：

```text
DEEPSEEK_API_KEY=你的密钥
```

### 3. 启动

Windows（PowerShell）：

```powershell
.\helperme-env\Scripts\python.exe console_chat.py
```

macOS / Linux：

```sh
./helperme-env/bin/python console_chat.py
```

首次启动会创建个人配置，默认使用 `deepseek/deepseek-v4-pro`。模型切换方法见[模型配置指南](docs/模型配置.md)。Python 环境位于项目目录的 `helperme-env`，可直接删除此目录清理环境；Agent 命令中的 Python 也使用该环境。个人配置和会话保存在 `~/.helperme`。

## 文档

[文档索引](docs/README.md) · [架构总览](docs/架构/总览.md) · [自举开发](docs/自举开发.md)
