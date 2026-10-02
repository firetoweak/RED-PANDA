# helperMe

面向个人使用的 AI 助手。

## 快速开始

准备 ripgrep，以及一个模型来源：[DeepSeek API Key](https://platform.deepseek.com/api_keys)、[OpenAI API Key](https://platform.openai.com/api-keys)、阿里云百炼 Qwen API Key、[阶跃星辰 StepFun API Key](https://platform.stepfun.com/)、[智谱 BigModel API Key](https://bigmodel.cn/)，或本地运行的 vLLM / Ollama。不需要预装 Python 或 Docker。

### 1. 安装

在仓库目录运行对应平台脚本。脚本会下载项目专用 Python、安装依赖，并在个人数据目录 `~/.helperme` 创建 `config.json` 与 `connections.json`（已存在则不覆盖）。

Windows（PowerShell）：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

macOS / Linux：

```sh
sh scripts/setup.sh
```

### 2. 写入模型密钥

打开 `~/.helperme/connections.json`，填写所用供应商的 `api_key`；本地 vLLM / Ollama 填写 `base_url`。文件会创建全部支持的供应商配置项。默认 DeepSeek 的部分如下（其余供应商保留在文件中）：

```json
"deepseek": { "api_key": "你的密钥" }
```

连接配置热更新，保存后下一次调用直接生效。Web 可以在密钥未填写时启动。

### 3. 启动 Web

首次需要 Node.js 安装并构建前端：

```sh
cd web
npm install
npm run build
cd ..
```

Windows（PowerShell）：

```powershell
.\helperme-env\Scripts\python.exe web_chat.py
```

macOS / Linux：

```sh
./helperme-env/bin/python web_chat.py
```

浏览器打开 http://127.0.0.1:8765，侧栏进入「模型配置」管理候选模型与新会话默认值，再在会话输入区选择模型。同一供应商可以添加多个模型；切换在下一次决策生效，已有历史保留。详细配置见[模型配置指南](docs/模型配置.md)。

终端入口仍可运行 `console_chat.py`。Python 环境位于项目目录的 `helperme-env`；个人配置和会话保存在 `~/.helperme`，可用 `HELPERME_HOME` 指定其他个人数据目录。

## 文档

[文档索引](docs/README.md) · [架构总览](docs/架构/总览.md) · [自举开发](docs/自举开发.md)
