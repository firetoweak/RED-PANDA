# VFS 数据库执行层迁移验证

2026-10-10。主项目已从 Turso 切换到 tokio-rusqlite / rusqlite / bundled SQLite，
并通过正式构建脚本生成、安装了新的 Windows release sandbox。
本记录覆盖这次迁移；早期 `.tools` 候选记录中的“尚未合并”已不代表当前状态。

## 实际变化

保留 FileSystem 的异步接口、CoW、内容寻址 chunk、Journal、快照与历史重放。
同步 SQL、完整事务及其提交后清理在专用连接线程执行，不按 SQL 语句拆分异步提交。
tokio-rusqlite 使用上游 `call_raw`，没有维护该库或 SQLite 的本地分支。

执行器负责连接配额和结果交付。调用者取消等待后，已接纳的操作继续完成；
配额不会提前释放，原始错误或 panic 会保留到后续操作或收尾屏障观察。
已有异步 ReapHook 在数据库线程通过捕获的 Tokio runtime 等待，事务不离开该线程。
未知错误、损坏与契约违规停止当前执行器，不能通过缓存读取伪装为正常状态。
已知的文件系统拒绝及 SQL 约束拒绝在事务回滚后可继续使用连接。

Batcher 保留其批处理职责，提交后才移除对应 pending 前缀并发布结果。
导入只在提交成功后发布目录映射；读取将数据库快照和同代 pending 视图合并。
并发 WAL checkpoint 单独串行，普通 SQL 不经过这个检查点锁。
`finalize` 停止 batcher、排空、检查点并等待执行器屏障；这不等于关闭所有共享连接线程。
私有历史重放使用显式 close，冻结制品使用 immutable 只读连接。

Windows 只读 URI 保留规范化路径的 `\\?\` 前缀，先编码为 URI 路径，再由
SQLite 的 `win32-longpath` 文件接口打开。回归覆盖普通／规范化路径、中文、空格、
`#`、`%` 和超过 300 字符的路径，并检查未修改制品或生成 WAL／SHM。

性能处理保留前期定位的三处改动：空闲连接直接提交 worker，减少额外 Tokio task；
chunk 热读借用 SQLite blob，避免额外复制；恢复原有 mimalloc 分配器。
正式构建固定 `-DSQLITE_DIRECT_OVERFLOW_READ=0`，使大 chunk 重复读取使用页缓存。
项目根 Cargo 配置与 sandbox 构建脚本均设置该参数，BUILD.json 记录它及源码摘要。
fsync 仍使用原有 FULL 同步路径，随后恢复 NORMAL，没有以取消同步来换取吞吐。

依赖固定为 tokio-rusqlite 0.8.0、rusqlite 0.40.2、libsqlite3-sys 0.38.2
（SQLite 3.53.2）；实际依赖以两个 Cargo.lock 为准。
移除了 307 个受 Git 跟踪的 Turso vendored 文件，合计 364,240 行，以及对应 Cargo patch。
VFS 锁文件包数从 331 降至 173，sandbox 从 289 降至 109；这些是锁文件中的包数，
不表示每个平台实际编译全部包。新增依赖许可证随程序分发。

持久格式仍是 0.11；没有旧引擎分支、迁移兼容 API 或实验 fixture 功能进入生产代码。
原测试恢复正常 Cargo 自动发现，需私有访问的测试位于 `tests/internal/`。

## 正式项目回归

使用 D 盘已有便携 Rust 1.99.0-nightly、LLVM MinGW、Cargo 离线缓存与 WinFsp SDK。
target 为 `x86_64-pc-windows-gnullvm`；只为构建子进程设置环境，没有修改系统／用户 PATH。

| 检查 | 结果 |
| --- | --- |
| VFS core / mount 普通 Rust 测试 | 205 通过；8 个挂载、崩溃及 worker 用例默认 ignored |
| 显式串行运行真实原生用例 | 5 通过：挂载、属性、I/O 错误、写入崩溃、执行进程崩溃；worker 由主测试启动 |
| `python -m pytest` | 703 通过、3 跳过、168 按默认 marker 筛除 |
| 指定 sandbox / assistant 产品 process 测试 | 78 通过、4 个 Linux 专用用例跳过 |
| Rustfmt 与严格 Clippy | 通过；WinFsp 外部 C 头文件仍有 13 条 pragma 警告 |
| 结构检查与 DDL census | 通过；schema 外 DDL 为 0，路径包含的测试文件计 5 个 |
| 正式 sandbox release 离线构建 | 成功，exe、DLL、许可证及 BUILD.json 已更新 |

Rust 测试覆盖事务取消、首次打开失败、原始 panic／错误保留、导入回滚、
损坏数据暴露、并发快照、历史重放、白化、HostFS CoW 和冻结制品。
产品 process 测试覆盖 Python → sandbox → WinFsp，包括 coding 与分支恢复。
崩溃用例验证进程异常退出后的恢复，不代表进行了断电或物理介质故障测试。

本次在 Windows 上执行了 `consistency-canon.sh` 内的全部 Python 检查块和 DDL census；
没有运行 Linux `gate.sh`，也没有把默认 ignored 的结果算作真实挂载通过。
本地完整日志位于 `.tools/sqlite-verified-{rust,native-serial,process,clippy,canon}.log`，
日常 Python 日志为 `.tools/sqlite-python-default.log`，构建日志为 `.tools/sqlite-delivery-build.log`。

## 原版数据验证

未改动的 Turso 基线程序创建格式 0.11 制品，包含 inline → chunked 历史、硬链接、
符号链接、跨 chunk CoW 与白化。链接正式 SQLite 代码的程序直接只读校验，
再对私有副本重放回较早的 inline root；HostFS 内容保持不变。

原制品 SHA256 为 `64bebc110e1c2b9f2b89958c563fba7bf3270353a520f47ae12b2de6925c9156`，
验证前后完全一致，WAL／SHM 均不存在。证据为 `.tools/vfs-mount-ab/compatibility.json`。
这是当前格式的实际样本互通验证，不是对全部历史数据库格式的兼容承诺。

## 实际 Windows 挂载性能

同一份 `.tools/vfs-mount-ab/main.rs`、相同配置、allocator、Tokio 线程数、数据和操作序列，
两个独立 release 程序分别链接原 Turso 代码与主项目迁移后的 SQLite 代码。
基线来自提交 `da305ddf12a3bfece611203277c0bd0abaa1fadf`。
每引擎三轮，顺序为 T/S、S/T、T/S；以下数值为每轮指标的中位数。
测量期间本任务没有同时编译或运行 pytest／其他挂载测试。

| 负载 | ops/s：Turso → SQLite | p95 毫秒：Turso → SQLite |
| --- | ---: | ---: |
| 4 KiB 打开／读／关闭 | 1256.5 → 2841.4 | 0.993 → 0.497 |
| 64 KiB 打开／读／关闭 | 1035.3 → 2719.2 | 1.171 → 0.537 |
| metadata 查询 | 1607.4 → 4541.1 | 0.769 → 0.330 |
| 创建／写／同步／改名／删除 | 37.3 → 92.8 | 29.645 → 12.609 |
| 7 字节 CoW 写／同步／关闭 | 321.4 → 584.5 | 3.669 → 2.119 |
| 四客户端混合：读 | 974.6 → 1786.7 | 5.106 → 3.155 |
| 四客户端混合：写＋同步 | 243.7 → 446.7 | 5.770 → 3.327 |

这组负载吞吐为原来的 1.82～2.83 倍，p95 下降 38%～57%。
单客户端每轮读 4 KiB 1500 次、64 KiB 750 次、metadata 3000 次、创建链路及 CoW 各 300 次。
读之前预热 128 次，每次实际 open/read/close；保留适配器原有 FlushAndPurgeOnCleanup。
混合阶段四个原生客户端各执行 600 次请求，80% 读、20% 写并 sync_all，
吞吐按整个阶段 wall time 计算，分别统计读与写的 p95。
CoW 阶段第一次写触发 copy-up，随后重复写同一对象，不是每次都新建 CoW 文件。

这是适配器、Windows I/O、路径、元数据和数据库的整体成本，不是纯 SQL 或冷盘吞吐。
前期纯 core 四客户端实验曾测到读 p95 约 15% 回退；本轮 WinFsp 回调持有 State 锁，
不能用挂载结果证明那个并发路径的问题已经消失。
Linux FUSE、macOS、MSVC、更高并发与长时间压力仍未验证。

六轮原始指标、中位数汇总及制品校验哈希已归档到
[results.json](VFS_SQLite迁移数据/results.json)，对比程序源码归档到
[mount-ab.rs](VFS_SQLite迁移数据/mount-ab.rs)。汇总逐项核对了三轮中位数。
归档源码只将 CRLF 统一为 LF，数据中分别记录测量源码与归档源码的 SHA256。
本机 stdout／stderr 保存在 `.tools/vfs-mount-ab/`，早期实验另存于 `pre-migration/`。
基线二进制 SHA256：`a59bd57b876725271e8592f8f8c82b1057f082b6bcc203feec275dae7290faec`。
正式代码候选二进制 SHA256：`fa7b8926bca0460786d21c80bea603ae531cd3ff6cbe584f210b080a1a931874`。
一次性运行脚本和原版二进制留在忽略的实验目录；归档源码只供核对实验，
不参与生产 Cargo workspace，也不构成双引擎路径。

## 复现入口

常规构建、测试与五个显式原生用例命令见
[VFS TESTING](../../native/vfs/docs/TESTING.md)。当前本机便携环境可使用：

```powershell
$py = 'D:/work/helpMe/RED-PANDA/redpanda-env/Scripts/python.exe'
& $py .tools/with-native.py cargo.exe test --manifest-path native/vfs/Cargo.toml -p vfs-core -p vfs-mount --features winfsp --lib --tests --locked --offline --target x86_64-pc-windows-gnullvm -- --test-threads=1
& $py .tools/validate_sqlite_native.py
& $py .tools/with-native-release.py sandbox
$env:REDPANDA_SANDBOX_EXECUTABLE = (Resolve-Path redpanda/sandbox/bin/redpanda-sandbox.exe).Path
& $py -m pytest
& $py -m pytest -m process tests/sandbox/file_view tests/sandbox/test_vfs_workspace_process.py tests/sandbox/test_linux_workspace_execute.py tests/assistant/test_vfs_coding_process.py
& $py .tools/vfs-mount-ab/compatibility.py
& $py .tools/vfs-mount-ab/run.py
```

性能对比前需确认其 candidate 已重新链接当前主项目且其他负载已经停止。
上述 `.tools` 辅助文件依赖本机路径，不随 Git 分发；原生回归和生产构建入口保留在仓库中。

从远程仓库重新进行性能实验时，分别检出上面的基线提交与本迁移分支，
在仓库外建立两个独立 Cargo 工程，共用归档的 `mount-ab.rs`。
package 名分别使用 `mount-ab-baseline` 和 `mount-ab-candidate`，
将 `vfs-core`、启用 `winfsp` 的 `vfs-mount` 路径依赖指向对应检出的源码。
公共依赖为 `tokio`（full）、`tempfile`、`serde_json`、`anyhow`、`libc`。
基线工程的 `[patch.crates-io]` 还须指向基线中的三个 `third_party/turso*` 目录；
候选工程设置上述 SQLite 编译参数。双方使用相同工具链与 release profile。
按 T/S、S/T、T/S 顺序运行，各传入轮次 `0`、`1`、`2`，程序最后一行输出 JSON。
以 `results.json` 中的操作数和指标口径核对工作负载，再计算各引擎三轮指标的中位数。
二进制哈希仅标识本次测量文件，不要求另一台机器重新构建得到逐字节相同的程序。
