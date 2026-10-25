# 安装、初始化与启动

从源码准备环境、初始化个人目录与模型、日常启动与开发模式启动。来源：`scripts/setup.sh`、`setup.ps1`、`setup.py`、`scripts/runtime_tools.py`、`runtime_tools.json`、`konden/initialize.py`、`scripts/dev.sh`、`dev.ps1`、`scripts/start.sh`、`start.ps1`、`scripts/launch.py`，文档 `docs/运行指南.md`、`docs/开发指南.md`。

## Sub-features

- `setup`：`sh scripts/setup.sh`（Windows 为 `scripts/setup.ps1`）先装固定版本 uv、下载 `.python-version` 指定的 Python、建 `konden-env`，再跑 `python -m scripts.setup` 四步：
  - `[1/4] 准备应用命令行工具`：PATH 上缺失的工具（Node 还要求版本受支持且有 npm）装进 `.tools/runtime/`：Node、rg、fd，Windows 另有 MinGit；Chrome for Testing 固定使用应用专用的 `.tools/runtime/chrome/`，不复用日常浏览器。Linux 缺 `git` 时经系统包管理器安装；macOS 需提前安装 Git，支持 Apple Silicon 与 Intel，浏览器应用包使用系统 `ditto` 解包。不安装驱动或其他系统组件；版本与下载校验以 `scripts/runtime_tools.json` 为准。
  - `[2/4] 安装 Python 依赖`、`[3/4] 安装浏览器能力与前端依赖并构建网页`（`scripts/browser` 与 `web` 分别 `npm ci`，再 build 网页）、`[4/4] 初始化配置`（`python -m konden.initialize`，带 `--configure` 时进向导）。
  - 成功末行 `运行环境准备完成。使用 scripts/start.ps1 或 scripts/start.sh 启动，在网页侧栏配置模型。`；子命令失败打印 `准备未完成（命令退出码 N）。请处理上方错误后重新执行。`；下载、校验或文件操作错误打印 `准备未完成：<具体原因>` 并退出 1。`setup.sh` 接受 Linux/macOS，其他系统打印 `Use setup.sh on Linux/macOS or setup.ps1 on Windows.` 退出 1。
- `setup-flags`：`setup.py` 只有 `--configure`（安装后在终端配模型；默认在网页配）与 `--check`。`--check` 先把 `.tools/runtime/` 下已有工具加到本进程 PATH 前面，再查 Git/rg/fd/Node/npm，Node 须 22.x（≥22.13）或 24.x；不通过时打印 `系统依赖未就绪：` 加各项（如 `Node.js 版本不支持；请使用 22.x（至少 22.13）或 24.x，或重新运行准备命令自动安装。`）退出 1，通过时打印 `系统依赖检查通过。`。
- `start`：`sh scripts/start.sh` → `scripts.launch` → `web_chat.py`。
- `initialize`：`python -m konden.initialize --configure`（交互添加/修改默认模型）、`--if-needed`（缺默认模型或连接时进入向导）；两者互斥。
- `dev`：`sh scripts/dev.sh` → `scripts.launch --dev` → `web_chat.py --dev`。

## How to get to it (user POV)

- 新克隆仓库后按 `README.md` / `docs/运行指南.md` 依次执行。

## Driving it with the shell

在一次性目录（新 clone + 临时 `KONDEN_HOME`）里做，避免破坏现有环境。

- **setup**：
  - (a) `konden-env/bin/python --version`（Windows `konden-env\Scripts\python.exe`）的主次版本与 `.python-version` 一致；`$KONDEN_HOME` 下有 `config.json`、`connections.json`；空 home 初始化后没有 `workspaces.json`，首次登记工作区后才产生；PATH 缺工具时 `.tools/runtime/` 下出现对应版本。
  - (b) 依次打印 `[1/4]`…`[4/4]` 与成功末行，退出码 0。
  - (c) 已有的 `config.json`、`connections.json` 不被覆盖（先写入标记内容再跑一次，逐字节比较）。
  - (d) 从 PATH 去掉 `rg` 再跑 **不会**报错：`[1/4]` 把 rg `14.1.1` 装进 `.tools/runtime/` 后继续。要看到失败路径，用下载失败（断网且 `.tools/runtime/` 缺工具）→ `[1/4]` 失败并以 `准备未完成…` 结束，不进入 `[2/4]`。
- **setup.py --check**：
  - (a) 直接用已有解释器跑 `python -m scripts.setup --check` 不装 Python 依赖、不跑 `npm`、不改 `.tools/runtime/`。
  - (b) 依赖齐全时 `系统依赖检查通过。` 退出 0。
  - (c) `.tools/runtime/` 里已有的工具算作可用（被加到 PATH 前面），不要求它们在系统 PATH 上。
  - (d) 系统 PATH 只有 Node 20 且 `.tools/runtime/` 没有 Node → `系统依赖未就绪：` + `Node.js 版本不支持…` 退出 1。注意 `sh scripts/setup.sh --check` 会先装 uv/Python 并创建 `konden-env`，再执行检查。
- **initialize**：
  - (b) `--if-needed` 已配置时打印 `默认模型与连接设置已填写，保留现有配置。` 退出 0。
  - (c) 已配置时 `--if-needed` 不进入向导、文件不变。
  - (d) 非交互终端（stdin/stdout 不是 TTY）跑 `--configure` → argparse 错误 `请在交互终端运行项目 Python -m konden.initialize --configure，或编辑上面的配置文件。` 退出 2；`--if-needed` 未配置但非 TTY 时只打印同一句、退出 0。
- **start / dev**：
  - (d) 缺 `konden-env`：`start.sh` 打印 `Run sh scripts/setup.sh first to prepare konden.`，`dev.sh`（还检查 `web/node_modules/vite/bin/vite.js`）打印 `Run sh scripts/setup.sh first to prepare the project environment.`，都退出 1。Windows 的 `start.ps1` / `dev.ps1` 抛出 `Run scripts\setup.ps1 first to prepare konden.` / `Run scripts\setup.ps1 first to prepare the project environment.`。
  - (d) 有 venv 但缺 `web/dist/index.html`（`--dev` 时缺 `web/node_modules/vite/bin/vite.js`）→ `scripts/launch.py` 退出并打印 `网页尚未准备好，请先运行 scripts/setup.ps1 或 scripts/setup.sh。`。

## Gotchas

- `setup.ps1` 需 Windows 机器。
- Linux 自动安装 `git` 会调用 `apt-get`/`dnf`，可能要 sudo 密码；无人值守环境提前装好。
- 2026-10-18 改写：准备流程从五步改为四步，不再下载原生运行包、不装驱动；旧的构建与运行包条目已删除。未 live 复跑。
- 2026-10-20 Windows 隔离 worktree：`setup.ps1` 四步完成，项目 Python 3.13.15、前端构建及 `python -m scripts.setup --check` 退出 0；空 home 仅有配置与数据目录，未登记前没有 `workspaces.json`。Web 启动结果见下方维护实测；交互向导与 Linux 分支未覆盖。
- 历史验证：2026-10-12（main@1a4790a）与 2026-10-16（main@e191eab）验证了当时的五步准备流程；`setup.py --check` 在 Node 20.19 上按预期报版本不支持。

- 2026-10-20 维护实测（`origin/main@32860f6`，Windows Chrome/ConPTY，隔离 home）：Windows setup 四步、setup --check、隔离 Web 启动与 Doctor 全部通过，浏览器首页可用；启动与 Doctor 分开执行成功。Linux、交互模型向导及 start/dev 包装入口未覆盖。

- 2026-10-22 全量维护（`origin/main@4c000ca`，Windows Chrome/ConPTY，隔离 home、真实 DeepSeek）：Windows setup 四步、setup --check、隔离 Web 启动、Doctor 与首页通过；Linux、首次模型向导和开发启动未重验。

- 2026-10-23 浏览器增量：Windows 独立 worktree，按清单校验并安装应用专用 Chrome；`python -m scripts.setup` 四步完成，实际执行两份 npm 锁文件安装及网页构建，新 home 配置初始化成功。内置浏览器真实调用见 extensions。Linux 压缩包已核对下载校验与程序权限信息，未实跑 Linux；未重跑 PowerShell 的 Python 引导层与交互向导。
