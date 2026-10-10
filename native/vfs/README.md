# RED PANDA 的 VFS 存储与挂载库

这份代码由 Factory VFS 分支裁剪而来，供 RED PANDA 的原生 sandbox 使用。
来源、上游版本和本地补丁见 [SOURCE.md](SOURCE.md)，许可证保留在 `licenses/`。

```text
native/sandbox → vfs-mount → vfs-fuse → vfs-core
                    └────────────────→ vfs-core
```

`vfs-core` 拥有 SQLite 文件状态、本地内容寻址 chunk、HostFS、overlay/COW、
冻结数据库制品和内部历史机制；`vfs-fuse` 负责 Linux FUSE 传输；
`vfs-mount` 负责 Linux FUSE 与 Windows WinFsp 挂载的建立和结束。
macOS HostFS 保留，但当前 sandbox 没有 macOS 挂载执行入口。

不再提供独立 VFS CLI、NFS、会话交接、KV、工具调用审计、数据库加密配置或远端 chunk 源。
VFS 只负责私有文件视图；操作记录、制品引用和向用户工作区发布由 `native/sandbox`
及 RED PANDA 的 Python 适配层负责。私有文件视图本身不提供进程或网络隔离。

数据库和显式配置的只读 base 是文件视图的依据。缓存和句柄不能成为第二事实源。
虚拟写入只修改 delta，不能修改 host base；承诺持久化的 Flush/fsync 必须等提交完成。
冻结父制品必须只读打开，缺失或损坏须暴露失败。

数据库执行由 `tokio-rusqlite` 的专用连接线程承载，完整事务在一次后台操作中完成。
取消等待不会取消已经提交给线程的操作；执行器保留连接配额和原始错误直到收尾完成。
SQLite 随源码依赖编译，不再维护数据库引擎的本地分支。

当前持久格式为 **0.11**，旧格式直接拒绝，不迁移或保留兼容入口。
本次裁剪不调整 journal、根快照、历史范围及重放算法；与它们耦合的内部元数据暂留，
后续单独讨论其职责。

构建与验证见 [TESTING.md](docs/TESTING.md)，格式见 [SPEC.md](docs/SPEC.md)，
Windows 已知边界见 [WINDOWS.md](docs/WINDOWS.md)。

## License

MIT。上游来源和第三方许可证继续随原生 sandbox 分发。
