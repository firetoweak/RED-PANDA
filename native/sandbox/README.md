# RED PANDA 原生文件管理服务

本目录维护 RED PANDA 的 Rust 文件视图应用：候选操作、变化证据、接受与恢复。Python 文件管理入口在 `redpanda/sandbox/files/`，原生客户端与宿主发布在其内部 `file_view/`；契约测试在 `tests/sandbox/file_view/`。Factory 的通用 COW 与 Windows/WinFsp 挂载源码已合入相邻的 `native/vfs/`，来源见 [VFS 源码记录](../vfs/SOURCE.md)。这两块源码由同一个 RED PANDA 仓库维护，保留文件系统与应用的职责边界。

## 调用与职责

```text
Assistant / Host：业务意图、委派与验收
  → sandbox/files：操作编排、宿主发布、恢复与子任务成果交换
  → redpanda-sandbox：候选、变化证据、封存、接受、恢复
  → 仓库内 vfs-core / vfs-mount：COW 数据库、文件语义
       Windows 使用 WinFsp，Linux 使用 FUSE
```

Python 启动本地 Rust 子进程，通过标准输入／输出的逐行 JSON 发送控制操作；文件内容不走 JSON。`begin` 创建候选并返回挂载路径，文件工具用普通文件 IO 访问该路径，命令在投影 cwd 中执行。执行结果封存后接受并发布；失败返回也可能产生实际文件变化，未知异常保留未决状态并原样暴露，不自动重跑。

恢复由 Assistant 解析工具调用或时间线位置，Python 编排恢复候选、接受与发布。模型恢复是工具调用；用户时间旅行的文件结果作为分支起点事实保存。底层 VFS 库不认识 Session、Step 或模型语义。两种恢复策略与共享任务根的边界见[文件操作与回退](../../docs/架构/Sandbox/文件操作与回退.md)。

## 构建

支持 Windows x64 与 Linux。macOS 尚未实现。需要 Python 与 Rust。VFS 源码已随本仓库提供，无需另行克隆 Factory 或旧实验应用。

Rust 为 `nightly-2026-08-07`（rustc `1.99.0-nightly`）。`native/sandbox/rust-toolchain.toml` 与相邻 VFS 使用同一工具链，并包含 rustfmt 和 clippy。

### Windows

需要 WinFsp 驱动及包含 Developer 特性的 SDK、Rust gnullvm 宿主工具链和 LLVM MinGW。SDK 的库名是 `winfsp-x64.lib`。只给其他宿主工具链添加 gnullvm target，不足以让宿主构建脚本使用同一 LLVM 链接环境。

用 rustup 准备工具链（会改变 rustup 默认宿主；已有便携工具链时沿用其配置）：

```powershell
rustup set default-host x86_64-pc-windows-gnullvm
rustup toolchain install nightly-2026-08-07 --profile minimal --component rustfmt,clippy
rustup target add x86_64-pc-windows-gnullvm --toolchain nightly-2026-08-07
```

在当前 PowerShell 中准备构建环境，LLVM 路径按实际安装位置填写：

```powershell
$llvm = 'C:\tools\llvm-mingw\bin'
$env:Path = "$env:USERPROFILE\.cargo\bin;$llvm;${env:ProgramFiles(x86)}\WinFsp\bin;$env:Path"
$env:CARGO_TARGET_X86_64_PC_WINDOWS_GNULLVM_LINKER = "$llvm\x86_64-w64-mingw32-clang.exe"
$env:CC_x86_64_pc_windows_gnullvm = "$llvm\x86_64-w64-mingw32-clang.exe"
$env:AR_x86_64_pc_windows_gnullvm = "$llvm\llvm-ar.exe"
$env:LIBCLANG_PATH = $llvm
$env:WINFSP_INCLUDE_DIR = "${env:ProgramFiles(x86)}\WinFsp\inc"
$env:WINFSP_LIB_DIR = "${env:ProgramFiles(x86)}\WinFsp\lib"
.\redpanda-env\Scripts\python.exe scripts/build_sandbox.py --runtime-dir $llvm
```

以上从 RED PANDA 根目录执行，默认生成 `x86_64-pc-windows-gnullvm` release 程序。`--debug` 选择 debug，`--offline` 适用于 Cargo 依赖已下载的环境。SDK 不在默认位置时，使用 `--winfsp-include`、`--winfsp-lib` 或上面的环境变量指定。

SDK include 目录应包含 `winfsp/winfsp.h`，lib 目录应包含当前 linker 可使用的 WinFsp 链接库。`--runtime-dir` 所指目录应包含 `libunwind.dll`，其上一级应包含 LLVM 的 `LICENSE.TXT`。

### Linux

编译不链接 libfuse。运行挂载需要 `fuse3` 提供的 `fusermount3`，以及当前用户可读写的 `/dev/fuse`。开发包与 `native/vfs/scripts/install-deps.sh` 一致。

Ubuntu / Debian：

```sh
sudo apt-get update
sudo apt-get install -y fuse3 libfuse3-dev pkg-config build-essential
```

CentOS / RHEL / Fedora：

```sh
sudo dnf install -y fuse3 fuse3-devel pkgconf-pkg-config gcc
```

仍使用 yum 的发行版：

```sh
sudo yum install -y fuse3 fuse3-devel pkgconfig gcc
```

非特权挂载：

- `ls -l /dev/fuse` 应为当前用户可读写。常见权限是 `crw-rw-rw-`。节点不存在时执行 `sudo modprobe fuse`。权限为 `crw-rw----` 且属组为 `fuse` 时，执行 `sudo usermod -aG fuse "$USER"` 并重新登录。
- 普通用户通过 `fusermount3` 挂载，不需要 root。
- 沙箱挂载的 `allow_other` 默认为关闭，不读取 `user_allow_other`。只有挂载点要给其他用户访问时，才在 `/etc/fuse.conf` 取消 `user_allow_other` 的注释。

准备后，在 RED PANDA 根目录执行：

```sh
./redpanda-env/bin/python scripts/build_sandbox.py
```

Linux 构建宿主 target 的 release 程序；`--debug` 和 `--offline` 与 Windows 含义相同。

Cargo 直接使用 `../vfs/` 的仓库内路径依赖，数据库由 tokio-rusqlite 与随包编译的 SQLite 提供；没有外部源码路径、子模块或 Junction。构建临时目录在本项目 `.tools/` 内；`CARGO_TARGET_DIR` 可指定其他构建输出位置。

## 运行与分发

输出在 `redpanda/sandbox/bin/`。Windows 文件名是 `redpanda-sandbox.exe`，同目录还有 WinFsp 用户态 DLL，以及 GNU LLVM target 需要的运行库。Linux 文件名是 `redpanda-sandbox`。Python 默认从这个目录启动程序，无需旧实验包、旧实验目录的 PATH 或额外配置；`REDPANDA_SANDBOX_EXECUTABLE` 可显式选择另一个构建。

`BUILD.json` 记录当前 VFS、Rust 应用、构建脚本的源码 SHA256 和输出文件 SHA256，不依赖另一个仓库的 Git 状态；附带的 `licenses/` 保存构建所用依赖的声明与 VFS 首次导入来源。生成文件不提交到源码库。发布 Windows 二进制时，需一并携带同目录 DLL、构建来源和许可证声明，运行机器仍需安装 WinFsp 驱动。发布 Linux 二进制时，运行机器仍需 `fusermount3` 和可访问的 `/dev/fuse`。当前脚本不安装系统组件。

## 范围与验证

这是任务文件状态回退机制，命令继承用户现有开发环境，不提供完整操作系统隔离。挂载外写入、环境安装、系统配置和网络副作用不属于回退范围。目录重命名、硬链接命名空间改动、ACL、ADS 等仍有明确能力限制。

产品测试从仓库根目录运行，原生进程测试串行执行：

```sh
python -m pytest
python -m pytest -m process tests/sandbox/file_view tests/sandbox/test_vfs_workspace_process.py tests/sandbox/test_child_files_process.py tests/sandbox/test_subagent_worktrees_process.py tests/assistant/test_vfs_coding_process.py
```

Windows 需要已构建程序与 WinFsp；Linux 需要已构建程序、`fusermount3` 和可访问的 `/dev/fuse`。未满足条件的测试会跳过，不能据此声称已运行验证。Linux 命令与边界还可显式运行 `tests/sandbox/test_linux_workspace_execute.py`、`tests/sandbox/test_linux_sandbox_edges.py`。

底层 Rust 测试、挂载与崩溃测试见[VFS 测试说明](../vfs/docs/TESTING.md)。产品设计从[Sandbox 总览](../../docs/架构/Sandbox/总览.md)进入；有日期的实验与验证记录从[文档索引](../../docs/README.md)查阅。
