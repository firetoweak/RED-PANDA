# RED PANDA 原生文件视图

本目录维护 RED PANDA 的 Rust 文件视图应用：候选操作、变化证据、接受与恢复。Python 客户端、宿主发布及事务编排在 `redpanda/sandbox/file_view/`；契约测试在 `tests/sandbox/file_view/`。Factory 的通用 COW 与 Windows/WinFsp 挂载源码已合入相邻的 `native/vfs/`，来源见 [VFS 源码记录](../vfs/SOURCE.md)。这两块源码由同一个 RED PANDA 仓库维护，保留文件系统与应用的职责边界。

## 调用与职责

```text
Assistant 的工具执行 / 会话时间旅行
  → Python sandbox：操作身份、事务编排、路径投影、宿主发布
  → redpanda-sandbox.exe：候选、变化证据、封存、接受、恢复
  → 仓库内 vfs-core / vfs-mount：COW 数据库、文件语义、WinFsp 文件视图
```

Python 启动本地 Rust 子进程，通过标准输入／输出的逐行 JSON 发送控制操作；文件内容不走 JSON。`begin` 创建候选并返回挂载路径，文件工具用普通文件 IO 访问该路径，命令在投影 cwd 中执行。执行结果封存后接受并发布；失败返回也可能产生实际文件变化，未知异常保留未决状态并原样暴露，不自动重跑。

恢复由 Assistant 解析工具调用或时间线位置，Python 编排恢复候选、接受与发布。模型恢复是工具调用；用户时间旅行的文件结果作为分支起点事实保存。VFS 不认识 Session、Step 或模型语义。两种恢复策略与共享任务根的边界见[工作区版本](../../docs/架构/运行/工作区版本.md)。

## 构建

当前只支持 Windows x64。需要 Python、Rust、WinFsp 驱动和 SDK。VFS 源码已随本仓库提供，无需另行克隆 Factory 或旧实验应用。仅安装 WinFsp 运行时不包含 SDK 的头文件与链接库。

本机 Rust 为 `1.99.0-nightly`，对应 VFS 中的 `nightly-2026-08-07`。使用 rustup 时需在 `native/sandbox/` 选择同一工具链（相邻 VFS 的工具链文件不会自动作用到 sandbox），并安装所选 Windows target、rustfmt 和 clippy。默认 MSVC target 需要 Visual Studio C++ 工具链，在其开发者终端运行构建。GNU LLVM target 需要 LLVM MinGW 的编译器和链接器在 PATH，并配置对应的 Cargo linker。

在 RED PANDA 根目录执行：

```powershell
python scripts/build_sandbox.py
```

默认使用 `x86_64-pc-windows-msvc` release 构建，需要相应 Rust target 和 Visual Studio C++ 工具链。SDK 不在安装目录时，通过 `--winfsp-include`、`--winfsp-lib` 显式指定；也可使用 `WINFSP_INCLUDE_DIR`、`WINFSP_LIB_DIR`。

本机已实际验证的是 `x86_64-pc-windows-gnullvm` debug 构建，使用 LLVM MinGW、对应 target 的 WinFsp 链接库，以及 `--runtime-dir <LLVM工具链bin目录>`。MSVC 构建尚未在本机验证。`--target`、`--debug`、`--offline` 可显式选择构建方式。

GNU LLVM 的构建参数示例（SDK 和运行库路径由调用方提供，Rust/C 编译器仍需提前准备）：

```powershell
python scripts/build_sandbox.py --target x86_64-pc-windows-gnullvm --debug `
  --winfsp-include $env:WINFSP_INCLUDE_DIR --winfsp-lib $env:WINFSP_LIB_DIR `
  --runtime-dir $env:LLVM_RUNTIME_DIR
```

SDK include 目录应包含 `winfsp/winfsp.h`，lib 目录应包含当前 linker 能使用的 `winfsp_x64` 链接库；运行库目录应包含 `libunwind.dll`，其上一级应包含 LLVM 的 `LICENSE.TXT`。`--offline` 仅适用于 Cargo 依赖已下载的环境。

Cargo 直接使用 `../vfs/` 的仓库内路径依赖，第三方 Turso 补丁也来自该目录；没有外部源码路径、子模块或 Junction。构建临时目录在本项目 `.tools/` 内；`CARGO_TARGET_DIR` 可指定其他构建输出位置。

## 运行与分发

输出在 `redpanda/sandbox/bin/`：`redpanda-sandbox.exe`、WinFsp 用户态 DLL，以及 GNU LLVM target 需要的运行库。Python 默认从这个目录启动程序，无需旧实验包、旧实验目录的 PATH 或额外配置；`REDPANDA_SANDBOX_EXECUTABLE` 可显式选择另一个构建。

`BUILD.json` 记录当前 VFS、Rust 应用、构建脚本的源码 SHA256 和输出文件 SHA256，不依赖另一个仓库的 Git 状态；附带的 `licenses/` 保存构建所用依赖的声明与 VFS 首次导入来源。生成文件不提交到源码库。发布二进制时，需一并携带同目录 DLL、构建来源和许可证声明，运行机器仍需安装 WinFsp 驱动；当前脚本不安装系统组件。

## 范围与验证

这是任务文件状态回退机制，命令继承用户现有开发环境，不提供完整操作系统隔离。挂载外写入、环境安装、系统配置和网络副作用不属于回退范围。目录重命名、硬链接命名空间改动、ACL、ADS 等仍有明确能力限制。

正常运行 `python -m pytest`；原生进程契约使用 `python -m pytest --all tests/sandbox/file_view tests/sandbox/test_vfs_workspace_process.py tests/assistant/test_vfs_coding_process.py`。需要已构建程序与 WinFsp，未满足条件时跳过相应测试。原生挂载测试串行执行。产品接入验证及限制见 `docs/验证/VFS接入.md`。
