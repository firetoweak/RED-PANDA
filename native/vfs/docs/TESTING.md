# 构建与测试

本文只负责底层 VFS 库、挂载适配器及原生故障测试。工具链准备、产品构建与 Python 文件管理测试统一见[原生服务说明](../../sandbox/README.md)。

工具链固定为 `nightly-2026-08-07`；从 `native/vfs/` 执行以下 Rust 命令。挂载与崩溃测试串行运行，跳过或仅编译成功不代表运行验证。

## Windows x64

先按[原生服务的 Windows 构建说明](../../sandbox/README.md#windows)准备 gnullvm、LLVM MinGW 与 WinFsp SDK 环境，然后执行：

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
显式运行原生服务说明中的 process 测试。macOS 仅保留核心 HostFS；本地 Windows 验证不覆盖它。

## SQLite 构建约定

项目根 `.cargo/config.toml` 与 sandbox 构建脚本固定
`LIBSQLITE3_FLAGS=-DSQLITE_DIRECT_OVERFLOW_READ=0`。64 KiB chunk 读取必须走
SQLite 页缓存；启用 direct overflow read 会绕过缓存，使重复读取明显变慢。
在本仓库目录外调用 Cargo 时，也须设置同一变量；构建记录保存实际编译参数。

数据库回归包括执行器取消、首次连接打开失败、原始 panic/错误交付、导入回滚、
并发冻结制品、内容块回收与失败回滚。未知错误使当前执行器停止；故障恢复检查须显式重新打开。
这不替代上面的真实挂载、进程终止恢复与 Python process 测试。
