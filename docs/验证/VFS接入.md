# Windows VFS 接入验证

> 历史验证记录。本文保留 2026-10-09 接入及后续 Linux 验证的当时记录。源码路径、工具链说明和测试数字不是当前使用手册；SQLite 执行层、底层历史移除及子任务增量交接均有后续变化。当前职责见[Sandbox 总览](../架构/Sandbox/总览.md)，构建见[原生服务](../../native/sandbox/README.md)。

2026-10-09。本轮将已经实验过的文件视图应用接入 RED PANDA，验证真实 Worker 编码、命令文件结果和会话时间旅行。现行设计契约见[文件操作与回退](../架构/Sandbox/文件操作与回退.md)，本文记录实现与验证事实。

## 已接入的路径

`redpanda/sandbox/versions.py` 使用本仓库 `redpanda/sandbox/file_view/` 的候选、封存、接受、发布和恢复接口，Rust 应用源码在 `native/sandbox/`。同一任务根使用共享操作历史和跨进程串行所有权。初始化与 Step 边界只读取操作元数据，不再通过 Git 枚举、复制整个任务树。

生产装配将文件工具、命令工作目录及子会话成果比较／合入放入同一个候选视图。内部枚举、grep 和 Git 发现的挂载路径转换回逻辑任务路径，再使用原有权限边界校验。命令返回失败仍记录实际文件结果；未知异常留下未决状态并原样暴露，不重跑命令。已封存结果可以按操作身份继续接受和发布。

模型的 `restore_workspace` 支持 `preserve` 和 `original`，默认前者。前者保留用户后续值，后者允许将助手改变的位置恢复到原值；两者保留助手未改变的位置。恢复本身可撤销，依据的是恢复执行前的实际内容。用户时间旅行仍走 `restart_from_step`，文件处理保存为分支起点的领域事实，不新增补偿 Command；后续模型回退以这个实际起点为基准。

日常 Git 快照路径已移除。`redpanda/sandbox/worktrees.py` 只保留显式 SubAgent 工作树复制、比较和合入，因此这类业务操作仍可能遍历项目；不能把“日常边界不扫描”扩展为任何操作都不扫描。

## 本机修正

首次接入时，文件视图应用在 `D:\work\helpMe\vfs-workspace` 更新到 0.4.1；其 Rust、Python 应用源码与 65 项原生契约测试现已迁入 RED PANDA。旧目录仅保留历史实验与证据，产品不再导入旧 Python 包。Rust 控制端与 Python 客户端使用 Windows 长路径存储，活动挂载移到 store 的固定位置，避免任务身份与操作身份叠加导致挂载路径超过普通路径限制。首次接入未修改 Factory；源码收敛阶段的 Factory 修正与验证另见下节。

完整回归发现内部原子元数据替换的瞬时拒绝访问。独立 2000 次替换实验出现 8 次错误 5，约 5–6 毫秒后可成功；持有不允许删除共享的读句柄可复现同类错误。Rust 与 Python 只对尚未发布的内部元数据替换，在 Windows 错误 5／32 时等待 10、20、40、80 毫秒后重试。持续占用及其他错误仍原样失败；不重做命令或宿主文件写入。尚未确认造成瞬时占用的具体外部进程。

父子会话 live 测试又发现旧 Git 业务路径的长路径缺口。Python 文件访问使用长路径形式，传给 Git 的参数使用其自身支持的普通路径形式，hash 输入使用显式文件路径。生成的子工作树目录改为父子身份共同形成的一个完整 Base32 摘要，减少嵌套，不截断摘要。超过普通路径限制的深层文件读取、记录、合入已有专项验证；这不意味着 Git for Windows 支持任意长度的工作树根目录。

投影内的 `read-tree -u` 还暴露了目录解析问题：Git 能判断自己位于工作树内，但其更新工作树的目录切换失败。查询 [Git 的 setup_work_tree](https://github.com/git/git/blob/master/setup.c) 和 [Git for Windows 的 mingw_chdir](https://github.com/git-for-windows/git/blob/main/compat/mingw.c) 后，结合实测将问题收敛到启用符号链接时的原生目录解析路径。只在投影内设置 `core.symlinks=false` 后合入成功，普通宿主工作树复制仍使用原设置；没有改变用户 Git 配置。这与当前投影不支持 reparse 的能力边界一致。

模型父子会话合入后，`get_changes` 还出现对象文件路径过长。读取 Git 变化的命令统一使用 `core.longpaths=true`，仍禁用可选锁与 index 自动刷新。原生发现测试加入深 store 场景，使挂载内对象路径明确超过普通路径限制；live 验收同时要求 Git 结果成功且包含实际新增文件，不再只验证合入成功。

## 首次接入验证结果

| 验证 | 结果 | 本机证据 |
| --- | --- | --- |
| RED PANDA 默认与进程回归（产品 Python 环境） | 796 passed、136 subtests passed、8 skipped；约 193 秒 | `tests/.live_workspace/sandbox-all-final-02` |
| 文件工具、投影、真实命令与回撤专项 | 77 passed、23 subtests passed | `tests/.live_workspace/sandbox-native-final-02` |
| 独立文件视图应用 | 65 项通过 | `D:\work\helpMe\vfs-workspace\evidence\redpanda-integration-final-01` |
| 子成果投影合入与 Windows 深层文件 | 6 项通过 | `tests/.live_workspace/sandbox-subagent-mount-06` |
| 深 store 的 Git 读取与发现路径 | 19 项通过 | `tests/.live_workspace/sandbox-git-longpath-01` |
| Web | 类型检查、27 个测试文件／132 项测试及生产构建通过 | 本轮前端命令结果；构建仅有 bundle 大小提示 |
| Rust 应用 | fmt、build、clippy `-D warnings` 通过 | `vfs-workspace/redpanda-integration-{fmt-02,build-04,clippy-02}.json` |

跳过项为 6 项 POSIX Bash 与 2 项 Windows 符号链接权限相关测试。完整回归另有 2 条 BytesIO 回收的 BufferError 警告，已在此前 Worker／Compact 回归中出现，本轮未定位其来源。原生挂载测试串行运行，新建证据与临时目录均位于 D 盘，没有再次创建巨型稀疏文件或修改系统驱动。

测试结束后的 `fsptool lsvol` 返回成功且无 WinFsp 卷，进程清单中无 `workspace-view.exe`。C 盘剩余约 53.5 GiB，D 盘约 289.8 GiB；证据为 `tests/.live_workspace/sandbox-final-inventory-03.json`。

真实模型在独立产品数据目录、真实 Worker 中读取错误代码，用 `apply_patch` 修复，执行 Python unittest，核对命令生成且被 `.gitignore` 忽略的文件；随后调用 Web 所用的时间旅行入口恢复助手改动，保留用户另建文件。两次模型分别为 DeepSeek V4 Pro 与 Flash，均通过。Flash 在产品 Python 环境中约 1.40 秒开始调用工具，完整编码过程约 10.39 秒，文件时间旅行约 1.36 秒。Pro 完整编码过程约 9.47 秒，时间旅行约 1.46 秒。

Flash 证据位于 `tests/.live_workspace/sandbox-live-flash-01`，Pro 位于 `sandbox-live-01`。这里计量的是端到端工具开始与编码完成时间，没有测量浏览器首个流式文字时间，也没有做切换会话后的 Web 延迟统计，因此不能据此宣称原始延迟问题已全面解决。

真实父子 Worker 的闭环也已通过：子 Agent 加载并调用只读 MCP 获取随机标记，在自己的投影写入文件；对父绝对路径的写入被拒绝。父在授权之前看不到新增文件，验收 diff 后仅对 `merge_subagent` 授权，再以 `read_file` 与 `get_changes` 核对实际合入结果。用户 Git 的 HEAD 和 index 未改变。最终成功证据为 `tests/.live_workspace/sandbox-subagent-live-05`，约 66 秒；原生专项另验证合入结果可通过父投影回撤并保留用户另建文件。

## 源码收敛与重建

Rust 应用在 `native/sandbox/`，Python 客户端与发布编排在 `redpanda/sandbox/file_view/`，原来的 65 项契约归入 `tests/sandbox/file_view/` 的 process 层。通用 COW 与 Windows 挂载源码也已合入 RED PANDA 的 `native/vfs/`，保留独立的 Cargo workspace 与模块职责，不再需要外部 Factory checkout。构建方式见 `scripts/build_sandbox.py`。

VFS 导入来源是已发布的 `firetoweak/vfs` 提交 `87701622d9068fed65cc7fdbaef093dd20bf79d3`，保留源码、测试、文档和现有声明，排除 `.agents/` 历史实验目录，不带 `.git` 或本机构建输出，见[来源记录](../../native/vfs/SOURCE.md)。`scripts/build_sandbox.py` 直接构建仓库内源码，生成默认程序、同目录运行库和当前源码／输出的 SHA256；无需 Junction、外部源码路径或本地 Git 元数据。本机验证的是 GNU LLVM debug target。

重新构建后的 32 层测试暴露叠层名称查询重复向基底查找的问题。修正在 Factory 的名称解析与 lookup 中：一次查找复用已经解析的基底结果，不按 inode 映射反推名称，因为硬链接的多个名字可以共享 inode。新增独立的 32 层缺失名称查询和硬链接名字契约，覆盖复杂度与别名语义。

RED PANDA 历史重放测试同时修正了一处顺序假设：完成批次的工具结果可以跟在循环提示之后，契约改为原文仍保留且只出现一次，生产 LoopGuard 行为未变。回归 tokenizer 词表使用已经下载的 D 盘缓存，避免每个隔离临时目录触发重复下载。

| 源码收敛后的验证 | 结果 | 本机证据 |
| --- | --- | --- |
| 完整 default 与 process 回归 | 861 passed、155 subtests passed、8 skipped；约 259 秒，包含迁入的 65 项原生契约 | `tests/.live_workspace/sandbox-source-all-03` |
| Factory 身份与 Windows 文件系统契约 | 18 passed，包含 32 层缺失名称与硬链接名字专项 | `tests/.live_workspace/sandbox-source-factory-02` |
| Rust 应用与 Factory core 检查 | fmt、重新 build、clippy `-D warnings` 通过 | `tests/.live_workspace/sandbox-source-rust-03`，其中 `BUILD.json` 保存最终程序来源与 SHA256 |
| 真实 Flash 编码与会话时间旅行 | 1 passed；首个工具约 1.23 秒，编码约 9.73 秒，文件时间旅行约 1.21 秒，保留用户新增文件 | `tests/.live_workspace/sandbox-source-live-flash-02` |

以上产品回归使用本仓库默认程序、正常 PATH，未设置 `REDPANDA_SANDBOX_EXECUTABLE`，且旧 `workspace_view` 包已卸载。跳过项仍为 6 项 Bash 与 2 项 Windows 符号链接权限测试，仍有两条此前出现的 BytesIO 回收警告。本轮验证范围为 Windows，未运行 Factory 的 Linux/macOS 全量 gate。

真实模型步骤再次覆盖读取、补丁、命令实际运行 Python 测试、命令生成被 `.gitignore` 忽略的文件，以及 Web 使用的时间旅行入口。测试结束后无 `redpanda-sandbox.exe`／`workspace-view.exe` 进程，`fsptool lsvol` 成功且无挂载，清单保存在 `tests/.live_workspace/sandbox-source-inventory-01.json`。这仍是 Worker 与 Host 的闭环计量，不是浏览器流式首字或切换会话延迟测量。

## VFS 底层源码合入后的验证

`native/vfs/` 的 605 个导入文件逐字核对已发布 fork 提交，另有本仓库新增的来源记录；上游 `.agents/` 历史材料未纳入源码。Cargo metadata 确认 sandbox、VFS 和 Turso 补丁的全部本地依赖都解析到 RED PANDA 的 `native/` 内。旧 `.tools/factory-vfs` Junction 已移除，独立 `D:\work\vfs` fork 未修改。

从仓库内源码以 GNU LLVM debug target 重新构建，通过两个 Rust workspace 的格式检查和 sandbox 的 Clippy `-D warnings`；`BUILD.json` 中当前源码及输出文件 hash 已逐项复核。新程序以默认路径运行文件视图、工作区版本和 Assistant 编码／模型恢复进程测试，结果为 78 passed、19 subtests passed，约 77 秒。本轮未再次运行完整 Python 回归或真实模型调用；此前结果见上节。证据在 `tests/.live_workspace/sandbox-vfs-in-tree-01`。

## 本地试用与边界

本机入口为仓库根目录的 `start-vfs-dev.ps1`，执行 `./start-vfs-dev.ps1` 可启动 Web。脚本现在使用本仓库默认的原生程序与同目录 DLL，不再设置旧实验程序或 LLVM 的 PATH；仍使用独立的 `D:\work\helpMe\redpanda-vfs-dev-home`，只复制配置与连接文件，不复制旧会话。它是忽略的本机脚本，包含本机数据目录，不是通用部署入口。

产品数据必须位于任务根之外。旧 Git SHA 会话版本事实不兼容新操作引用，使用新数据目录；不保留旧回退兜底路径。`requirements.txt` 已移除旧应用 editable 依赖，产品 Python 环境的 `workspace-view` 0.4.1 已卸载。原生构建使用 `scripts/build_sandbox.py`，输出在 `redpanda/sandbox/bin/`，默认运行路径不查询 PATH。之前的独立应用交付包不再作为当前产品构建。

这一阶段提供文件视图与文件结果回退，进程启动、事务编排和宿主发布仍由 Python 负责。命令继承调用方真实 Python、PowerShell、CLI 和 PATH；挂载外的绝对路径写入、安装、缓存、系统配置和网络不在回退范围内，也没有因此获得操作系统安全隔离。当前不支持目录重命名、仅大小写重命名、硬链接命名空间改动、ACL、ADS、reparse 和完整 mmap 一致性。这些限制已进入模型环境说明。

同一任务根共享历史，从一个位置撤销后续操作可能涉及其间其他会话的助手变化。`preserve` 默认保护用户后续值，但结果 X→H→X 仍可撤销，因为归因基于当前结果与操作证据，不监测自然人身份。没有在本轮新增证据自动回收、完整环境快照或进程安全沙箱。

## Linux 接入

Linux 使用仓库里已有的 FUSE 后端（`vfs-mount` 的 `Backend::Fuse`），不链接 libfuse。Windows 的 WinFsp 路径保持原样。宿主文件身份在 Linux 上是 `unix:{st_dev:016x}:{st_ino:016x}:{birth_ns:016x}`，与 Python 发布端一致。`birth_ns` 来自 `statx` 的出生时间，chmod 和写入不会改变它；inode 被复用且时钟往前走时，新文件会得到新的出生时间。没有把 `ctime` 放进身份，因为写入和 chmod 会改它，原地编辑就会被当成换了文件。本机 overlay 上 `FS_IOC_GETVERSION` 返回 `ENOTTY`，所以没有用 inode generation。文件系统不提供出生时间时该字段为 0。ext4、xfs、btrfs 和当前内核上的 tmpfs 会提供出生时间。overlay 的时间粒度较粗，同一时刻删了再建成时出生时间可能不变，这时同设备、同 inode 的身份仍会撞上。`st_dev` 可能在重启后变化，身份比较只在同一次启动内进行。

### 构建与运行前提

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

另外需要 rustup 的 `nightly-2026-08-07`。在仓库根目录执行 `python scripts/build_sandbox.py`。程序写到 `redpanda/sandbox/bin/redpanda-sandbox`。

非特权挂载：

- `/dev/fuse` 必须存在且当前用户可读写。`ls -l /dev/fuse` 在本机是 `crw-rw-rw-`。节点缺失时执行 `sudo modprobe fuse`。若权限是 `crw-rw----` 且属组为 `fuse`，执行 `sudo usermod -aG fuse "$USER"` 并重新登录。
- `fusermount3` 由 `fuse3` 提供。普通用户用它挂载，不需要 root。
- 沙箱挂载不设置 `allow_other`，因此不需要 `/etc/fuse.conf` 里的 `user_allow_other`。该选项只在挂载点要给其他用户访问时才取消注释。

没有 `/dev/fuse` 或没有挂载权限时，文件视图进程测试会在挂载阶段失败。不依赖挂载的默认测试仍用 `python -m pytest` 运行。

进程测试：

```sh
python -m pytest -m process tests/sandbox/file_view tests/sandbox/test_vfs_workspace_process.py tests/sandbox/test_linux_workspace_execute.py tests/sandbox/test_linux_sandbox_edges.py tests/assistant/test_vfs_coding_process.py
```

`tests/sandbox/test_linux_workspace_execute.py` 覆盖一次工具调用经 `versions.execute` 的写入、修改、删除、接受并发布到宿主目录、用户改写后的冲突，以及 `original` / `preserve` 两种恢复。`tests/sandbox/test_linux_sandbox_edges.py` 覆盖符号链接与可执行位、发布期间的 rename 保存、fifo 拒绝后服务仍可用、SIGKILL 后的挂载清理，以及 `setsid` 子进程。

### 与 Windows 语义的差异

这些差异留在平台边界上，不改 Windows 行为。

- 已接受的产物在钉住期间去掉写权限位，同一用户的 `open(r+b)` 会失败；释放钉后恢复原来的权限。可写目录里的 `rename` 仍会成功，Windows 的独占共享能拦住这次重命名。
- 删除已打开文件在 Linux 上通常成功，所以清理忙碌产物不会得到 Windows 的 sharing violation（错误 32）。
- 大小写仅有差别的重命名在大小写敏感的文件系统上是一次真实重命名。Windows 上的拒绝测试在 Linux 跳过。
- 硬链接命名空间改动仍会被拒绝。Linux 在 unlink/rename/replace 时把错误交回命令进程；WinFsp 在 Cleanup 里提交删除且不能返回错误，所以 Windows 的 unlink 要到 finish 才让服务失败。
- 宿主上先删除再重建时，若出生时间不同，冲突原因是 `identity_changed`。不提供出生时间的文件系统上，同长度重建仍可能只报 `length_changed`。
- 未按页对齐的写入会让 FUSE 先读覆盖该范围的页。块证据测试因此允许比 Windows 多两页的宿主读取，仍然不读取整个大文件。
- 符号链接会进入证据并按链接本身发布和恢复，不跟随到目标。父目录路径上的符号链接仍然拒绝。Windows 上的 reparse point 仍整段拒绝。fifo 等其他类型会写成 `rejected.json`，`finish` 返回 `rejected`，服务进程不退出。其他请求错误返回 `{"status":"error"}`，同样不退出。
- 发布写入后会再比较打开句柄和路径的身份。编辑器若在写入期间用 rename 换掉文件，事务记为冲突，用户文件保留。
- 文件权限位进入证据。Linux 发布和回退时对打开的句柄 `fchmod`。Windows 不记录、不修改权限位。
- 命令进程是子收割者。`setsid` 离开进程组后，父进程一死就被收割者杀掉，僵尸由收割者 `wait`。leader 是否已退出看 `/proc` 里的状态，`killpg` 发生在 `wait` 回收该 pid 之前。本机 `waitpid`/`waitid` 的 `WNOWAIT` 返回 `EINVAL`，所以没有用它。
- 进程被 SIGKILL 后，下一次 `begin` 若发现挂载点返回 `ENOTCONN`，会执行 `fusermount3 -u -z` 再挂载。`auto_unmount` 仍不启用，因为它会连带打开 `allow_other`。

### 本机结果

Ubuntu 24.04，当前用户可读写 `/dev/fuse`（`crw-rw-rw-`），`fusermount3` 可用，沙箱挂载没有设置 `allow_other`。在 `native/sandbox` 执行 `cargo clippy --locked --all-targets -- -D warnings` 通过。该包没有 Rust 单测。

```sh
python -m pytest -m process tests/sandbox/file_view tests/sandbox/test_vfs_workspace_process.py tests/sandbox/test_linux_workspace_execute.py tests/sandbox/test_linux_sandbox_edges.py tests/assistant/test_vfs_coding_process.py
```

结果为 85 passed、3 skipped、19 subtests passed，约 37 秒。跳过的是 Windows PowerShell 工作流、Windows 独占共享，以及大小写仅有差别的重命名。`tests/sandbox/test_linux_workspace_execute.py` 的发布、冲突和两种恢复都通过。`tests/sandbox/test_linux_sandbox_edges.py` 的符号链接、可执行位、发布期间 rename、fifo 拒绝、SIGKILL 挂载清理和 `setsid` 子进程都通过。本轮没有跑真实模型测试，也没有在 CentOS 上复跑。

同一环境下默认 `python -m pytest` 为 700 passed、5 skipped，174 个 process 测试被默认排除，约 10 秒。`test_keeps_current_path_prefix_and_appends_missing_session_dirs` 原先失败是因为测试把带盘符的 Windows 路径交给 `os.pathsep` 切分；合并实现按平台分隔符工作，Windows 上分隔符是分号，盘符冒号不会被切开。Linux 用例改为不含冒号的路径，Windows 用例仍使用原来的盘符路径。损坏的 CAS 证据现在让恢复请求返回 `error`，服务继续接受后续请求，并且不会创建候选。
