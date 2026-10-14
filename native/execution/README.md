# Windows 任务命令运行组件

此目录拥有任务启动、进程内宿主拒写策略、子进程接入和 Job 回收。文件视图、文件服务与历史仍归 [`../filesystem/`](../filesystem/README.md) 和 Agent 文件系统。生产 Python 接入位于 `redpanda/execution/local/`。

支持普通 Windows x64 开发工具，通过 Microsoft Detours 拦截文件变更及受支持的进程创建；不是对抗恶意进程的操作系统隔离。`CreateProcessW/A` 经过挂起、策略传递和握手后放行。其他经过本 DLL 的 `NtCreateUserProcess`、旧 `NtCreateProcess/Ex` 入口拒绝执行；显式挂起、调试、脱离 Job 和 x86 子进程也被拒绝。代理服务及直接系统调用不在这一保证内。

主进程和子进程都使用真实 cwd，不改写任务路径。注入使用完整 DLL 路径；先按系统代码页无损编码，必要时使用 Windows 短路径。Unicode 任务根和本机中文带空格安装路径已经实测。若安装路径既不能无损表示，也没有可用短路径，则阻止启动；可将组件所在项目移至系统能表示的路径，或使用支持该路径的 Windows UTF-8 系统代码页。不会降级为未接入执行，也不修改项目 PATH 来寻找 DLL。

Python 给启动器提供独立状态管道，命令 stdout/stderr 不携带控制协议，应用自己的退出码也不用于推断接入状态。运行中的子进程接入故障或无法确认的路径解析会通过任务共享故障通道停止整个任务。生命周期故障和协议损坏原样暴露，不能包装成普通命令失败。

启动器只把标准输入、输出、错误句柄交给任务根进程，策略通过 Detours 载荷传递；子进程替换环境不会丢失策略。Python 的外层 Job 负责取消时结束启动器及其任务，启动器的内层 Job 负责正常退出后的后代回收。两者均确认活动进程归零后才返回文件操作调用方。

## 构建与交付

Detours 源码已固定并随仓库保存，来源和 MIT 许可见 [`vendor/detours/SOURCE.md`](vendor/detours/SOURCE.md)。原生构建离线使用现有 LLVM MinGW，不在普通安装或命令执行时编译。

在项目根目录单独构建命令组件：

```powershell
python scripts/build_execution.py --llvm-dir .tools/llvm-mingw-20261006-ucrt-x86_64/bin
```

`scripts/build_sandbox.py` 的 Windows 构建也会构建命令组件。输出为 `redpanda/execution/bin/` 中的启动器、DLL、许可证和 `BUILD.json`；文件服务仍输出到自己的目录。

文件服务和命令组件各自形成运行包，使用同一固定发布版本，Windows CI 同时上传。命令组件包为 `redpanda-execution-windows-x86_64.zip`。准备入口 `python -m scripts.sandbox_runtime install` 在 Windows 安装两者，分别校验并替换；一个组件安装成功不会把另一个组件缺失视为可用。正常运行不需要编译器。自行构建的组件会被校验并保留；需要替换时使用 `install --replace`。

本次进程契约见 `tests/execution/`，按项目规范运行 `python -m pytest --all`。过程与限制见[生产接入验证](../../docs/验证/Windows命令链路接入.md)。
