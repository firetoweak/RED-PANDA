# RED PANDA 原生文件管理服务

为助手提供工作区文件视图，支持记录文件变化、恢复文件状态和交换子任务成果。服务由 Rust 实现，由 Python 后端启动。

目前支持 **Windows x64 与 Linux x64**（glibc 2.35+），macOS 尚未实现。应用代码在本目录，底层文件系统库在 [`../vfs/`](../vfs/README.md)。

## 运行包

正常使用安装已编译的沙箱运行包，**不需要 Rust、LLVM 或开发 SDK**。项目准备脚本自动下载 [`release.json`](release.json) 固定版本的运行包，校验 SHA256 后安装；已有相同版本时跳过。首次运行包尚待发布，发布前下载会明确报错。

安装位置为 `redpanda/sandbox/bin/`。Windows 包含程序、WinFsp 用户态 DLL 和 libunwind；Linux 包含程序。两种运行包都保留 `BUILD.json` 和许可证，Linux 安装时自动设置执行权限。

自行编译的程序会保留，不被准备脚本覆盖；修改过的下载包会提示你确认处理。需要明确替换为发布包时，使用自己的 Python 或项目运行环境执行：

```sh
python -m scripts.sandbox_runtime install --replace
```

下载或校验失败不会替换已有程序。不要只拷贝可执行文件而遗漏同包的运行库。

也可用 `REDPANDA_SANDBOX_EXECUTABLE` 指向其他位置的程序，其运行库仍须随程序完整保存。不要混用其他版本或历史实验的程序。

## 平台运行组件

### Windows

Windows x64 需要 WinFsp 驱动，项目准备脚本会自动检查并按需安装。单独准备驱动时，在仓库根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_winfsp.ps1
```

已有可用组件时直接跳过，不降级、不要求补装 SDK。首次安装按 Windows 提示确认管理员权限；需要重启时脚本会提示。安装日志在 `.tools/winfsp-install.log`。

### Linux

需要 `fuse3` 提供的 `fusermount3`。Ubuntu / Debian 使用 `sudo apt-get install -y fuse3`，Fedora / RHEL 使用 `sudo dnf install -y fuse3`。

`/dev/fuse` 必须存在且当前用户可读写，`/etc/mtab` 必须可读。设备不存在时由管理员加载 FUSE；设备权限不足时由管理员配置访问权限。容器或受限环境还需允许访问 FUSE 设备。

## 可选：开发原生组件

只有修改或自行编译本目录及 `native/vfs/` 的 Rust 源码，才需要 Rust 工具链、LLVM MinGW 和 WinFsp SDK。开发者自行准备编译环境，见[原生开发与构建](BUILD.md)。修改 Python 或前端代码可继续使用匹配的运行包。

## 能力边界

这是**工作区文件状态回退机制**，不提供完整操作系统隔离。命令继承用户现有开发环境；挂载外写入、环境安装、系统配置和网络副作用无法随文件一起回退。

具体文件操作限制见[构建与验证说明](BUILD.md#范围与验证)及 [VFS Windows 平台说明](../vfs/docs/WINDOWS.md)。

## 深入阅读

- [构建、分发与验证](BUILD.md)：工具链安装、路径参数、挂载排查和测试命令。
- [Sandbox 设计](../../docs/架构/Sandbox/总览.md)：文件管理、执行环境与子任务成果交换的职责。
- [底层 VFS 测试](../vfs/docs/TESTING.md)：Rust、挂载与崩溃测试。
