# 构建与测试

工具链固定为 `nightly-2026-08-07`。Python 产品测试从项目根目录运行：

```sh
python -m pytest
python -m pytest -m process tests/sandbox/file_view tests/sandbox/test_vfs_workspace_process.py tests/sandbox/test_linux_workspace_execute.py tests/assistant/test_vfs_coding_process.py
```

第二条必须先构建原生 sandbox；它检查真实私有视图、COW、冻结制品、发布、恢复和工具接入。
Windows 上 Linux 专用契约会跳过；不能据此声称 Linux 或 macOS 已验证。
挂载测试必须串行。

## Windows x64

需要 Rust gnullvm **宿主**工具链、LLVM MinGW 和 WinFsp SDK/运行库。只给 GNU 宿主
添加 gnullvm target 不足以让宿主构建脚本使用 LLVM 链接。
WinFsp 安装需包含 Developer 特性，SDK 的库名是 `winfsp-x64.lib`。

下面的 LLVM 路径按实际安装位置设置；这些设置也可以保存为用户环境变量。

```powershell
rustup set default-host x86_64-pc-windows-gnullvm
rustup toolchain install nightly-2026-08-07 --profile minimal --component rustfmt,clippy
$llvm = 'E:\myCard\RED-PANDA-build-tools\llvm-mingw-20261006-ucrt-x86_64\bin'
$env:Path = "$env:USERPROFILE\.cargo\bin;$llvm;${env:ProgramFiles(x86)}\WinFsp\bin;$env:Path"
$env:CARGO_TARGET_X86_64_PC_WINDOWS_GNULLVM_LINKER = "$llvm\x86_64-w64-mingw32-clang.exe"
$env:CC_x86_64_pc_windows_gnullvm = "$llvm\x86_64-w64-mingw32-clang.exe"
$env:AR_x86_64_pc_windows_gnullvm = "$llvm\llvm-ar.exe"
$env:LIBCLANG_PATH = $llvm
$env:WINFSP_INCLUDE_DIR = "${env:ProgramFiles(x86)}\WinFsp\inc"
$env:WINFSP_LIB_DIR = "${env:ProgramFiles(x86)}\WinFsp\lib"
python scripts/build_sandbox.py --debug --runtime-dir $llvm
```

构建脚本将 exe、WinFsp DLL、libunwind、许可证与 BUILD.json 一起复制到
`redpanda/sandbox/bin/`。不传 `--debug` 时生成 release 构建。
从 `native/vfs/` 运行核心和挂载测试：

```powershell
cargo fmt --all -- --check
cargo clippy -p vfs-core -p vfs-mount --features winfsp --all-targets --target x86_64-pc-windows-gnullvm -- -D warnings
cargo test -p vfs-core --target x86_64-pc-windows-gnullvm --lib --tests
cargo test -p vfs-mount --features winfsp --target x86_64-pc-windows-gnullvm --test windows_mount -- --ignored --test-threads=1
cargo test -p vfs-mount --features winfsp --target x86_64-pc-windows-gnullvm --test windows_attributes -- --ignored --test-threads=1
cargo test -p vfs-mount --features winfsp --target x86_64-pc-windows-gnullvm --test windows_io_errors -- --ignored --test-threads=1
cargo test -p vfs-core --target x86_64-pc-windows-gnullvm --test windows_write_crash -- --ignored --exact interrupted_writes_recover_whole_transactions_and_flushed_versions
cargo test -p vfs-mount --features winfsp --target x86_64-pc-windows-gnullvm --test windows_execution_crash -- --ignored --exact abrupt_execution_keeps_committed_view_and_host_unchanged
```

崩溃测试的 worker 由对应主测试启动，不要使用不带筛选的 `--include-ignored`。
已知平台、错误与崩溃边界见 [WINDOWS.md](WINDOWS.md)。

## Linux

需要可用的 `/dev/fuse`、FUSE 3 开发包、C 编译器及固定 Rust 工具链。
从项目根目录运行 `python scripts/build_sandbox.py`；从本目录运行：

```sh
scripts/gate.sh
```

该入口运行格式、Clippy、库测试和结构检查。原生挂载/产品流程仍须在构建 sandbox 后
显式运行上述 process 测试。macOS 仅保留核心 HostFS；本地 Windows 验证不覆盖它。

## SQLite 构建约定

项目根 `.cargo/config.toml` 与 sandbox 构建脚本固定
`LIBSQLITE3_FLAGS=-DSQLITE_DIRECT_OVERFLOW_READ=0`。64 KiB chunk 读取必须走
SQLite 页缓存；启用 direct overflow read 会绕过缓存，使重复读取明显变慢。
在本仓库目录外调用 Cargo 时，也须设置同一变量；构建记录保存实际编译参数。

数据库回归包括执行器取消、首次连接打开失败、原始 panic/错误交付、导入回滚、
并发快照和历史重放。未知错误使当前执行器停止；故障恢复检查须显式重新打开。
这不替代上面的真实挂载、进程终止恢复与 Python process 测试。
