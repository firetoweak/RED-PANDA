# RED PANDA 内置 VFS 的来源

本目录是纳入 RED PANDA Git 仓库的普通源码，不是子模块、目录联接或外部 checkout。
后续 VFS 修改与 sandbox 应用修改在 RED PANDA 中共同维护、验证和提交。

首次导入来源：

- Windows fork：https://github.com/firetoweak/vfs
- 提交：`87701622d9068fed65cc7fdbaef093dd20bf79d3`
- 上游：https://github.com/Factory-AI/vfs
- 上游基线：`4852148cecde9c5413b51e4184eac55015bfe766`
- 祖先：https://github.com/tursodatabase/agentfs

从该提交导入 Cargo workspace、第三方源码、测试、文档和现有许可证声明；排除上游 `.agents/` 历史实验目录（包含旧基准二进制和实验记录），不包含 `.git`、本机构建输出或实验数据。VFS 声明采用 MIT 许可，见 `Cargo.toml` 和 `README.md`；现有第三方声明保留在 `licenses/` 及第三方目录内。

Windows 支持范围以 `docs/WINDOWS.md` 为准。本地裁剪后保留 core、Linux FUSE、Windows WinFsp 与 macOS HostFS；独立 CLI、NFS、会话交接、KV、工具记录、加密配置和远端 chunk 源已移除。构建和测试入口见 `docs/TESTING.md`。

VFS 负责通用文件系统机制；RED PANDA 的候选操作、变化证据、接受与恢复应用在相邻的 `../sandbox/`，Python 事务编排在 `redpanda/sandbox/`。源码在一个仓库内，职责仍分开。

本文件记录首次导入来源，不表示后续本地修改仍与上述提交逐字相同。构建脚本为当前 VFS 与 sandbox 源码计算 SHA256，写入生成的 `BUILD.json`；从 GitHub 源码压缩包构建也不需要本地 Git 元数据。

数据库执行层已本地替换为 tokio-rusqlite / rusqlite 与 bundled SQLite。
首次导入的 `third_party/turso*` 源码及 Cargo patch 已移除；VFS 的持久格式、
CoW 与冻结制品保留；底层行级 Journal 和重放已移除，操作历史由 Sandbox 文件管理层持有。当前版本以 Cargo.lock 为准，新增依赖的许可证
及 SQLite 源码中的 public-domain 声明随原生程序一起分发。
