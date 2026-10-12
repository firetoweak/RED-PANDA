# 原生沙箱开发、构建与分发

**仅在修改或自行编译原生组件时需要本说明。** 运行 RED PANDA 使用编译好的沙箱程序，平台运行要求见[沙箱运行说明](README.md)。普通安装不需要下面的编译工具。

## 开发环境

编译工具和 Python 环境由开发者自行准备，不要求运行项目 `setup` 或使用 `redpanda-env`。下文 `python` 指向你选择的解释器；构建脚本只使用标准库，需要 Python 3.11 或更新版本。运行项目测试时，另需满足[项目开发要求](../../docs/开发指南.md#开发环境由你选择)。

## 系统依赖

支持 Windows x64 与 Linux，macOS 尚未实现。选择对应平台的章节操作。本节所有安装与检查命令均不依赖 `redpanda-env`。

Rust 使用项目固定的 `nightly-2026-08-07`，包含 rustfmt 和 clippy。系统工具需自行安装；VFS 源码已随仓库提供，无需另行克隆。

### Windows

以下为 Windows x64 原生组件的编译环境准备步骤。已有的组件可以跳过对应安装步骤。

#### 1. 安装 rustup，再安装 Rust 工具链

`rustup` 是 Rust 工具链管理器，需要先安装，才能执行 `rustup toolchain install`。在 PowerShell 中下载并运行[官方安装程序](https://rust-lang.github.io/rustup/installation/other.html)：

```powershell
Invoke-WebRequest -Uri 'https://static.rust-lang.org/rustup/dist/x86_64-pc-windows-msvc/rustup-init.exe' -OutFile "$env:TEMP\rustup-init.exe"
```

保留文件名 `rustup-init.exe`，安装程序依赖此名称识别安装模式。下载成功后，再运行：

```powershell
& "$env:TEMP\rustup-init.exe" -y --default-host x86_64-pc-windows-gnullvm --default-toolchain none
```

命令成功结束后才能继续；若出现 `error:`，先处理错误。这一步只安装管理器，指定后续使用 GNU LLVM 宿主；项目的 Rust 版本在下一条命令中安装。这个项目使用 LLVM MinGW，不需要为它安装 Visual Studio C++ 工具链。

**安装完成后，退出当前 Windows Terminal / PowerShell 窗口，再从开始菜单重新打开**，让安装程序写入的 PATH 生效，然后验证：

```powershell
rustup --version
```

应显示 rustup 的版本。确认成功后，安装项目要求的工具链并验证：

```powershell
rustup toolchain install nightly-2026-08-07-x86_64-pc-windows-gnullvm --profile minimal --component rustfmt,clippy
rustup run nightly-2026-08-07-x86_64-pc-windows-gnullvm rustc --version
```

需要安装完整的 gnullvm 宿主工具链，仅给其他 Rust 工具链添加 gnullvm target 不够。已有 rustup 时可跳过管理器安装；项目构建脚本会显式选择上述工具链，不改变已有的默认工具链。

#### 2. 下载并解压 LLVM MinGW

在仓库根目录打开 PowerShell，下载官方 [20261006 版本](https://github.com/mstorsjo/llvm-mingw/releases/tag/20261006)的 Windows x64 压缩包（约 191 MB）：

```powershell
Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/mstorsjo/llvm-mingw/releases/download/20261006/llvm-mingw-20261006-ucrt-x86_64.zip' -OutFile "$env:TEMP\llvm-mingw-20261006-ucrt-x86_64.zip"
```

下载成功后，解压到项目的 `.tools` 目录：

```powershell
Expand-Archive -LiteralPath "$env:TEMP\llvm-mingw-20261006-ucrt-x86_64.zip" -DestinationPath '.\.tools' -Force
```

验证编译器：

```powershell
& '.\.tools\llvm-mingw-20261006-ucrt-x86_64\bin\x86_64-w64-mingw32-clang.exe' --version
```

应显示编译器版本。无需管理员权限或手动加入 PATH，后面的构建命令使用此目录。已解压该版本且验证成功时，可跳过下载和解压。

#### 3. 安装 WinFsp 驱动和 SDK

在仓库根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_winfsp.ps1 -Developer
```

这里显式使用 `-Developer` 安装构建所需的 SDK。脚本自动下载官方安装包，出现 Windows 管理员确认时选择“是”。已有 WinFsp 时，用相同版本的安装包补齐 SDK，避免降级；组件齐全时直接跳过。无需手动选择版本或勾选组件。

脚本会检查 SDK 文件；失败时显示安装返回码，日志位于 `.tools/winfsp-install.log`。需要重启时会提示，不会自动重启电脑。默认安装目录为 `C:\Program Files (x86)\WinFsp`；已有自定义目录时，后续构建命令追加 `--winfsp-dir '实际安装目录'`。

### Linux

先安装 curl、C 构建工具和 FUSE，选择你的发行版对应的一组命令：

Ubuntu / Debian：

```sh
sudo apt-get update
sudo apt-get install -y curl fuse3 build-essential
```

CentOS / RHEL / Fedora：

```sh
sudo dnf install -y curl fuse3 gcc
```

仍使用 yum 的发行版：

```sh
sudo yum install -y curl fuse3 gcc
```

系统组件安装完成后，尚未安装 rustup 时，执行[官方安装命令](https://rust-lang.github.io/rustup/installation/index.html)，然后加载命令路径：

```sh
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- --default-toolchain none -y
. "$HOME/.cargo/env"
rustup --version
```

确认 rustup 可用后，再安装项目工具链：

```sh
rustup toolchain install nightly-2026-08-07 --profile minimal --component rustfmt,clippy
```

编译需要 C 构建工具，不链接 libfuse。运行挂载需要 `fuse3` 提供的 `fusermount3`，以及当前用户可读写的 `/dev/fuse`。

非特权挂载：

- `ls -l /dev/fuse` 应为当前用户可读写。常见权限是 `crw-rw-rw-`。节点不存在时执行 `sudo modprobe fuse`。权限为 `crw-rw----` 且属组为 `fuse` 时，执行 `sudo usermod -aG fuse "$USER"` 并重新登录。
- `/etc/mtab` 必须可读。Ubuntu 上它是指向 `/proc/self/mounts` 的符号链接。`fusermount3` 用它查找挂载项；这个文件不存在时卸载失败。
- 普通用户通过 `fusermount3` 挂载，不需要 root。
- 沙箱挂载的 `allow_other` 默认为关闭，不读取 `user_allow_other`。只有挂载点要给其他用户访问时，才在 `/etc/fuse.conf` 取消 `user_allow_other` 的注释。

## 首次构建

编译依赖已就绪后，使用自行准备的 Python，在仓库根目录执行对应平台的命令：

**Windows（PowerShell）**

```powershell
python scripts/build_sandbox.py --llvm-dir '.tools\llvm-mingw-20261006-ucrt-x86_64\bin'
```

**Linux**

```sh
python scripts/build_sandbox.py
```

Windows 命令使用上面解压的目录；已有其他 LLVM MinGW 安装时，将 `--llvm-dir` 改为它的 `bin` 目录。脚本自动准备编译器、链接器和 SDK 路径，默认生成 release 程序，并整理运行库与许可证。

## 重新构建

修改原生源码后，重复上面的构建命令即可。以下选项可用于构建和检查：

| 构建选项 | 用途 |
| --- | --- |
| `--llvm-dir` | Windows LLVM MinGW 的 `bin` 目录；已加入 PATH 时可省略 |
| `--winfsp-dir` | Windows WinFsp 安装根目录，包含 `bin`、`inc` 和 `lib` |
| `--check` | 只检查构建和运行依赖，不编译 |
| `--debug` | 构建 debug 程序 |
| `--offline` | 在 Cargo 依赖已下载时离线构建 |

这些选项用于 `scripts/build_sandbox.py`；项目环境准备脚本不接收沙箱路径参数。

Cargo 使用仓库内 `../vfs/` 路径依赖。构建临时目录在项目 `.tools/` 内；`CARGO_TARGET_DIR` 可指定其他构建输出位置。

## 运行与分发

输出在 `redpanda/sandbox/bin/`。Windows 文件名是 `redpanda-sandbox.exe`，同目录还有 WinFsp 用户态 DLL，以及 GNU LLVM target 需要的运行库。Linux 文件名是 `redpanda-sandbox`。Python 默认从这个目录启动程序，无需旧实验包、旧实验目录的 PATH 或额外配置；`REDPANDA_SANDBOX_EXECUTABLE` 可显式选择另一个构建。

`BUILD.json` 记录当前 VFS、Rust 应用、构建脚本的源码 SHA256 和输出文件 SHA256，不依赖另一个仓库的 Git 状态；附带的 `licenses/` 保存构建所用依赖的声明与 VFS 首次导入来源。生成文件不提交到源码库。发布 Windows 二进制时，需一并携带同目录 DLL、构建来源和许可证声明，运行机器仍需安装 WinFsp 驱动。发布 Linux 二进制时，运行机器仍需 `fusermount3` 和可访问的 `/dev/fuse`。构建脚本不安装系统组件。

## 发布运行包

原生源码变化会触发 [Sandbox runtime 工作流](../../.github/workflows/sandbox.yml)，分别在 Windows x64 和 Ubuntu 22.04 上构建 release 程序，打包运行库、构建记录、许可证及 SHA256，并执行真实安装与挂载验证。Linux 包要求 glibc 2.35 或更新版本。

需要发布新版本时，先更新本目录 `release.json` 的版本并提交到主分支，再到 GitHub Actions 的 **Sandbox runtime → Run workflow**，选择 `main` 并勾选 `publish`。两个平台都验证成功后，工作流创建 `sandbox-v<版本>` Release；已有版本不会被覆盖。普通 PR 或推送只构建验证，不发布。首次发布前，普通安装会明确提示运行包尚未发布。

发布只需在原生程序或通信协议变化时进行；Python、前端的普通修改继续复用固定版本。新的源码提交可以暂时保持同一个运行包版本，待原生功能准备好后再发布。

本地检查打包结果时，在完成 release 构建的仓库根目录执行：

```sh
python -m scripts.sandbox_runtime package --platform windows-x86_64
# Linux 使用 --platform linux-x86_64
```

产物在 `dist/sandbox/`；`BUILD.json` 的包版本只写入压缩包，本地构建仍被识别为本地构建。重新运行项目准备脚本会保留它；切回发布包需显式执行 `python -m scripts.sandbox_runtime install --replace`。

## 范围与验证

这是任务文件状态回退机制，命令继承用户现有开发环境，不提供完整操作系统隔离。挂载外写入、环境安装、系统配置和网络副作用不属于回退范围。目录重命名、硬链接命名空间改动、ACL、ADS 等仍有明确能力限制。

完成首次构建后，在满足[项目测试要求](../../docs/开发指南.md#测试)的自备 Python 环境中，从仓库根目录运行测试。原生进程测试串行执行：

```sh
python -m pytest
python -m pytest -m process tests/sandbox/file_view tests/sandbox/test_vfs_workspace_process.py tests/sandbox/test_child_files_process.py tests/sandbox/test_subagent_worktrees_process.py tests/assistant/test_vfs_coding_process.py
```

Windows 需要已构建程序与 WinFsp；Linux 需要已构建程序、`fusermount3` 和可访问的 `/dev/fuse`。未满足条件的测试会跳过，不能据此声称已运行验证。Linux 命令与边界还可显式运行 `tests/sandbox/test_linux_workspace_execute.py`、`tests/sandbox/test_linux_sandbox_edges.py`。

底层 Rust 测试、挂载与崩溃测试见[VFS 测试说明](../vfs/docs/TESTING.md)。产品设计从[Sandbox 总览](../../docs/架构/Sandbox/总览.md)进入；有日期的实验与验证记录从[文档索引](../../docs/README.md)查阅。
