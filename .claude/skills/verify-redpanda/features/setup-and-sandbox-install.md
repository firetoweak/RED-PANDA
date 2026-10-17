# 安装、初始化与沙箱构建

从源码准备环境、初始化个人目录与模型、构建或安装原生沙箱、开发模式启动。来源：`scripts/setup.sh`、`setup.ps1`、`setup.py`、`redpanda/initialize.py`、`scripts/build_agentfs.py`、`scripts/agentfs_runtime.py`、`scripts/install_winfsp.ps1`、`scripts/dev.sh`、`dev.ps1`、`scripts/start.sh`、`start.ps1`、`scripts/launch.py`、`scripts/runtime_tools.py`，文档 `docs/运行指南.md`、`docs/开发指南.md`。

## Sub-features

- `setup`：`sh scripts/setup.sh` 下载 `.python-version` 指定的 Python、建 `redpanda-env`，再跑 `setup.py` 五步：`[1/5]` 装发布版文件服务（`agentfs_runtime.py install --with-runtime`）、`[2/5]` 把固定版本 Node/rg/fd 装到 `.tools/runtime/`、`[3/5]` Python 依赖、`[4/5]` `npm ci` + build、`[5/5]` 初始化 `REDPANDA_HOME`；非 Linux 打印 `RED PANDA supports Windows x64 and Linux x64. Use setup.ps1 on Windows.` 退出 1。
- `setup-flags`：`setup.py --check`（只查 Git/rg/fd/Node/npm，Node 须 22.x（≥22.13）或 24.x，否则 `Node.js 版本不支持…` 退出 1）、`--configure`（在终端配模型；默认不进向导，在网页配）。
- `start`：`sh scripts/start.sh` → `scripts.launch` → `web_chat.py`；缺 `redpanda-env` 打印 `Run sh scripts/setup.sh first to prepare RED PANDA.`，缺 `web/dist/index.html` 打印 `网页尚未准备好…`。
- `initialize`：`python -m redpanda.initialize --configure`（交互添加/修改默认模型）、`--if-needed`（缺默认模型或连接时进入向导）。
- `build-sandbox`：`scripts/build_agentfs.py [--check] [--debug] [--offline] [--target] [--llvm-dir] [--winfsp-dir]`。
- `sandbox-runtime`：`scripts/agentfs_runtime.py install [--replace] [--with-runtime]`（复用或安装发布包；`--with-runtime` 检查 FUSE 或装 WinFsp 驱动）。
- `dev`：`sh scripts/dev.sh` → `scripts.launch --dev` → `web_chat.py --dev`，缺 `redpanda-env` 或 `web/node_modules/vite` 时打印 `Run sh scripts/setup.sh first to prepare the project environment.` 退出 1。

## How to get to it (user POV)

- 新克隆仓库后按 `README.md` / `docs/运行指南.md` 依次执行。

## Driving it with the shell

在一次性目录（新 clone + 临时 `REDPANDA_HOME`）里做，避免破坏现有环境。

- **setup**：(a) `redpanda-env/bin/python --version` 与 `.python-version` 一致；`$REDPANDA_HOME` 下有 `config.json`、`connections.json`、`workspaces.json`。(b) 退出码 0。(c) 已有的 `config.json`、`connections.json` 不被覆盖（先写入标记内容再跑一次，逐字节比较）。(d) 卸掉 `rg` 的 PATH 后运行 → 明确报缺少工具并非 0 退出，不留半成品 venv。
- **setup.py --check**：(c) 不创建 `redpanda-env`，只打印检查结果。
- **initialize --if-needed**：已配置时 (c) 不进入向导、文件不变；未配置时进入向导。
- **build_agentfs**：没有 `rustup` 时打印 `缺少 rustup：请先安装 Rust 工具链管理器。` 非 0。
- **build_agentfs --check**：缺 `fusermount3` 或 `/dev/fuse` 不可写时非 0 并说明原因。正常构建后 (a) `redpanda/filesystem/bin/` 下出现 `redpanda-agentfs` 二进制，Doctor 的 `sandbox.ready` 为 true。
- **agentfs_runtime install**：已有本地构建时不带 `--replace` → (c) 本地二进制不被替换；带 `--replace` → 被发布包替换（需网络）。(d) 断网时安装 → 明确报错，原二进制保留。
- **dev.sh**：未 `npm ci` 时报上面的提示并退出 1。

## Gotchas

- 本轮只读源码，全部 unverified-live；`setup.ps1`、`install_winfsp.ps1` 需 Windows 机器。
- 旧版本沙箱二进制与新代码不匹配时可能静默行为异常（上次扫描挖出过），Doctor 应能识别版本；重建后再验文件类功能。

## Live status
- 2026-10-16（main@e191eab）：Doctor 修正路径后 `sandbox.ready` true；`setup.py --check` 在 Node 20.19 上按预期报版本不支持；`build_agentfs.py` 无 rustup 时按预期报错（沙箱改用同源码构建的二进制）
- 通过（2026-10-12，main@1a4790a）：doctor 与 sandbox 构建通过
