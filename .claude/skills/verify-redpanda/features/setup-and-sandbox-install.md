# 安装、初始化与沙箱构建

从源码准备环境、初始化个人目录与模型、安装或构建原生沙箱、开发模式启动。来源：`scripts/setup.sh`、`setup.ps1`、`setup.py`、`scripts/runtime_tools.py`、`runtime_tools.json`、`redpanda/initialize.py`、`scripts/agentfs_runtime.py`、`scripts/execution_runtime.py`、`native/filesystem/release.json`、`scripts/build_agentfs.py`、`scripts/install_winfsp.ps1`、`scripts/dev.sh`、`dev.ps1`、`scripts/start.sh`、`start.ps1`、`scripts/launch.py`，文档 `docs/运行指南.md`、`docs/开发指南.md`。

## Sub-features

- `setup`：`sh scripts/setup.sh`（Windows 为 `scripts/setup.ps1`）先装固定版本 uv、下载 `.python-version` 指定的 Python、建 `redpanda-env`，再跑 `python -m scripts.setup` 五步：
  - `[1/5] 准备文件服务、任务命令组件与平台运行组件`：`agentfs_runtime.py install --with-runtime`，装 `native/filesystem/release.json` 指定版本（当前 `0.5.1`）的发布版文件服务；Windows 还安装执行运行包 `redpanda/execution/bin/redpanda-command.exe` 与 `redpanda-hook.dll`，并运行 `install_winfsp.ps1` 装 WinFsp 驱动；Linux 缺 `fusermount3` 时经 `apt-get`/`dnf` 安装 `fuse3`，再要求 `/dev/fuse` 可读写、`/etc/mtab` 可读。
  - `[2/5] 准备应用命令行工具`：PATH 上缺失的工具（Node 还要求版本受支持且有 npm）装进 `.tools/runtime/`：Node `24.22.0`、rg `14.1.1`、fd `10.2.0`，Windows 另有 MinGit；Linux 缺 `git` 时经系统包管理器安装。
  - `[3/5] 安装 Python 依赖`、`[4/5] 安装前端依赖并构建网页`（`npm ci` + build）、`[5/5] 初始化配置`（`python -m redpanda.initialize`，带 `--configure` 时进向导）。
  - 成功末行 `运行环境准备完成。使用 scripts/start.ps1 或 scripts/start.sh 启动，在网页侧栏配置模型。`；任一子命令失败打印 `准备未完成（命令退出码 N）。请处理上方错误后重新执行。`。非 Linux 跑 `setup.sh` 打印 `RED PANDA supports Windows x64 and Linux x64. Use setup.ps1 on Windows.` 退出 1。
- `setup-flags`：`setup.py` 只有 `--configure`（安装后在终端配模型；默认在网页配）与 `--check`。`--check` 先把 `.tools/runtime/` 下已有工具加到本进程 PATH 前面，再查 Git/rg/fd/Node/npm，Node 须 22.x（≥22.13）或 24.x；不通过时打印 `系统依赖未就绪：` 加各项（如 `Node.js 版本不支持；请使用 22.x（至少 22.13）或 24.x，或重新运行准备命令自动安装。`）退出 1，通过时打印 `系统依赖检查通过。`。
- `start`：`sh scripts/start.sh` → `scripts.launch` → `web_chat.py`。
- `initialize`：`python -m redpanda.initialize --configure`（交互添加/修改默认模型）、`--if-needed`（缺默认模型或连接时进入向导）；两者互斥。
- `build-sandbox`：`scripts/build_agentfs.py [--check] [--debug] [--offline] [--target] [--llvm-dir] [--winfsp-dir]`，面向开发者；普通用户不构建，`setup` 安装发布包。
- `sandbox-runtime`：`scripts/agentfs_runtime.py install [--replace] [--with-runtime]`（复用或安装发布包；`--with-runtime` 检查 FUSE 或装 WinFsp 驱动，不含 SDK）。
- `dev`：`sh scripts/dev.sh` → `scripts.launch --dev` → `web_chat.py --dev`。

## How to get to it (user POV)

- 新克隆仓库后按 `README.md` / `docs/运行指南.md` 依次执行。

## Driving it with the shell

在一次性目录（新 clone + 临时 `REDPANDA_HOME`）里做，避免破坏现有环境。

- **setup**：
  - (a) `redpanda-env/bin/python --version`（Windows `redpanda-env\Scripts\python.exe`）与 `.python-version` 一致；`$REDPANDA_HOME` 下有 `config.json`、`connections.json`、`workspaces.json`；`redpanda/filesystem/bin/BUILD.json` 的 `package.version` 为 `0.5.1`；PATH 缺工具时 `.tools/runtime/` 下出现对应版本。
  - (b) 依次打印 `[1/5]`…`[5/5]` 与成功末行，退出码 0。
  - (c) 已有的 `config.json`、`connections.json` 不被覆盖（先写入标记内容再跑一次，逐字节比较）；重跑时打印 `原生文件服务运行包 0.5.1 已就绪，跳过下载。`，不重新下载。
  - (d) 从 PATH 去掉 `rg` 再跑 **不会**报错：`[2/5]` 把 rg `14.1.1` 装进 `.tools/runtime/` 后继续。要看到失败路径，用下载失败（断网）→ `[1/5]` 打印 `原生文件服务准备失败：<原因>` 并以 `准备未完成（命令退出码 1）…` 结束，不进入 `[3/5]`。
- **setup.py --check**：
  - (a) 直接用已有解释器跑 `python -m scripts.setup --check` 不装 Python 依赖、不跑 `npm`、不改 `.tools/runtime/`。
  - (b) 依赖齐全时 `系统依赖检查通过。` 退出 0。
  - (c) `.tools/runtime/` 里已有的工具算作可用（被加到 PATH 前面），不要求它们在系统 PATH 上。
  - (d) 系统 PATH 只有 Node 20 且 `.tools/runtime/` 没有 Node → `系统依赖未就绪：` + `Node.js 版本不支持…` 退出 1。注意 `sh scripts/setup.sh --check` 会先装 uv/Python 并创建 `redpanda-env`，再执行检查。
- **initialize**：
  - (b) `--if-needed` 已配置时打印 `默认模型与连接设置已填写，保留现有配置。` 退出 0。
  - (c) 已配置时 `--if-needed` 不进入向导、文件不变。
  - (d) 非交互终端（stdin/stdout 不是 TTY）跑 `--configure` → argparse 错误 `请在交互终端运行项目 Python -m redpanda.initialize --configure，或编辑上面的配置文件。` 退出 2；`--if-needed` 未配置但非 TTY 时只打印同一句、退出 0。
- **build_agentfs**：普通构建与 `--check` 都先做依赖检查，不通过时打印 `原生依赖未就绪：` 加各项并退出 1。
  - (d) 没有 `rustup` → 含 `缺少 rustup：请先安装 Rust 工具链管理器。`。Linux 还检查 `cc`、`fusermount3`（`缺少 <命令>；请安装 C 构建工具与 fuse3。`）、`/dev/fuse` 可读写、`/etc/mtab` 可读；Windows 检查 LLVM MinGW（PATH 或 `--llvm-dir`）与 WinFsp Developer SDK（`--winfsp-dir`）。
  - (a) `--check` 通过时打印 `原生构建与运行依赖检查通过。`；正常构建后 `redpanda/filesystem/bin/` 下出现 `redpanda-agentfs` 二进制，Doctor 的 `sandbox.ready` 为 true。
- **agentfs_runtime install**：
  - (c) 已有本地构建（`BUILD.json` 没有 `package`）时不带 `--replace` → 打印 `保留本地构建的原生文件服务：<路径>`，二进制不被替换；带 `--replace` → 被发布包替换（需网络）。
  - (d) 断网或下载失败 → `原生文件服务准备失败：<原因>` 退出 1，原二进制保留；只有发布地址返回 404 时原因才是 `当前版本的原生文件服务运行包尚未发布，请查看仓库 Releases；无需安装 Rust 来运行。`；校验和不符 → `原生文件服务下载包 SHA256 校验失败；原有程序未更改。`。
- **start / dev**：
  - (d) 缺 `redpanda-env`：`start.sh` 打印 `Run sh scripts/setup.sh first to prepare RED PANDA.`，`dev.sh`（还检查 `web/node_modules/vite/bin/vite.js`）打印 `Run sh scripts/setup.sh first to prepare the project environment.`，都退出 1。Windows 的 `start.ps1` / `dev.ps1` 抛出 `Run scripts\setup.ps1 first to prepare RED PANDA.` / `Run scripts\setup.ps1 first to prepare the project environment.`。
  - (d) 有 venv 但缺 `web/dist/index.html`（`--dev` 时缺 `web/node_modules/vite/bin/vite.js`）→ `scripts/launch.py` 退出并打印 `网页尚未准备好，请先运行 scripts/setup.ps1 或 scripts/setup.sh。`。

## Gotchas

- 全部 unverified-live；`setup.ps1`、`install_winfsp.ps1`、执行运行包需 Windows 机器。
- 旧版本沙箱二进制与新代码不匹配时可能静默行为异常（上次扫描挖出过）。Doctor 不读版本，只看二进制是否存在与 FUSE 条件；版本要自己读 `redpanda/filesystem/bin/BUILD.json` 的 `package.version`，与 `native/filesystem/release.json` 的 `version` 对照，不一致就 `install --replace` 或重建后再验文件类功能。
- Linux 自动安装 `fuse3`/`git` 会调用 `apt-get`/`dnf`，可能要 sudo 密码；无人值守环境提前装好。
- 历史验证：2026-10-12（main@1a4790a）doctor 与 sandbox 构建通过；2026-10-16（main@e191eab）Doctor 修正路径后 `sandbox.ready` true，`setup.py --check` 在 Node 20.19 上按预期报版本不支持，`build_agentfs.py` 无 rustup 时按预期报错。2026-10-18 按源码改写，未 live 复跑。
