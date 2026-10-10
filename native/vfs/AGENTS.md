# VFS 本地工作约定

此目录是 RED PANDA 使用的本地 VFS 库；产品方向以项目根目录的 AGENTS.md 和
`docs/架构/` 为准，来源记录见 SOURCE.md。

依赖关系：`vfs-mount → vfs-fuse → vfs-core`，其中 FUSE 仅用于 Linux；
Windows 挂载实现位于 vfs-mount。macOS HostFS 保留。
文件系统语义属于 core，传输适配器只做协议转换。

必须保持：

- 所有可写虚拟文件状态由数据库承载；HostFS 只读，overlay 写入不能触及 host base。
- 缓存、挂载和句柄可重建；持久化确认只能在数据库提交后返回。
- 冻结制品只读打开，禁止原地修改。未知异常、损坏数据和契约违规必须暴露。
- 新测试放 `crates/<crate>/tests/`，只守明确的契约；已有内联测试不作为新增测试的模板。
- DDL 只在 `vfs-core/src/schema/`；环境读取只在配置边界或构建脚本。
- FUSE 导出面保持 `mount`、`FuseMountOptions`、`SessionHandle`；生产日志使用 tracing。
- 保持 `clippy::await_holding_lock = "deny"`；多锁模块保留锁顺序说明。
- 生产 Rust 文件不超过 2500 行非测试代码。不要引入旧设计兼容层。

当前范围不包含独立 CLI、NFS、KV、工具审计、会话交接、加密配置和远端 chunk 获取。
文件操作历史统一由 Sandbox 的文件管理层持有。底层只管理当前文件状态、SQLite 事务持久化与冻结制品，不保留独立的行级 journal、关系快照和历史重放。SQLite 自身的 WAL 与崩溃恢复必须保留。

测试命令和平台边界见 [docs/TESTING.md](docs/TESTING.md)。Linux 可运行
`scripts/gate.sh`；结构检查仍由 `scripts/validation/consistency-canon.sh` 和 DDL census 执行。
挂载与崩溃测试串行运行，不把成功编译或被跳过的测试报告成运行验证。
