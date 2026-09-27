# helperMe

事件为唯一事实、状态可完整重放的个人通用助手

> [!WARNING]
> 项目仍在积极开发中。接口、配置、存储格式和已有数据都可能发生不兼容变更，升级前请自行备份需要保留的数据。

## 一个由事实驱动的 Agent Runtime

HelperMe 的核心是一条可持久化、可恢复、可追溯的执行循环：

```text
Event → State → Step → Command → Outcome → Event
```

运行中发生的一切都先成为 Journal 中不可原地修改的 Event。State 不是另一份可变数据，而是这些事实的确定性归约结果。模型每次只推进一个 Step；需要影响外部世界时提交 Command，执行结果作为新的 Event 回到下一轮决策。

**Journal 是唯一的执行事实源。** 模型上下文、摘要和诊断视图都只是投影，可以丢弃和重建，不能反过来改写事实。

这套设计直接带来四个能力：

- **完整重放**：整条 Event 流可以从头归约，能在任意历史切面重建当时的 State；恢复不依赖进程内缓存。
- **因果追溯**：一次决策看到了哪些事实、发出了哪些 Command、得到了什么 Outcome，都能沿 Journal 还原，不靠日志猜测。
- **诚实恢复**：进程重启后只从已提交事实继续。已经开始却没有结果的外部操作保持“未知”，不会被伪装成未执行或盲目重试。
- **投影可重建**：模型上下文、Trace 和未来的摘要都可以随时重新生成，优化或损坏投影不会改变真实执行历史。

**Runtime 不替模型理解世界。** 它只负责归约、调度、不变量和安全边界；目标是否满足、事实意味着什么、下一步做什么，始终交给模型、显式 Judge 或用户判断。

MCP、Skill、SubAgent 等能力也不会侵入 Runtime。它们各自通过窄边界接入，并按需进入模型上下文，使助手不断增长时，基础执行内核仍然保持稳定和可理解。

完整设计见[架构总览](docs/架构/总览.md)和[Runtime](docs/架构/运行/Runtime.md)。

## 运行

开发和运行环境统一使用 Python 3.13.x，目前主要在 Windows 上开发和测试。

内置 `grep` 工具依赖外部 [ripgrep](https://github.com/BurntSushi/ripgrep#installation)（命令名 `rg`），需要单独安装，`requirements.txt` 不会安装它。Windows 可使用：

```powershell
winget install --id BurntSushi.ripgrep.MSVC --exact
```

安装后重新打开终端，执行 `rg --version` 验证。确保 `rg` 所在目录已加入本机用户的 `PATH`，让 HelperMe Worker 能找到它；已运行的 HelperMe 需重新启动。缺少该依赖时，调用 `grep` 会返回 `RG_NOT_FOUND`。

安装 Python 3.13 后，创建虚拟环境并启动：

```powershell
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python console_chat.py
```

如果已有 `.venv` 是用其他 Python 版本创建的，先移走旧环境，再按上述命令重建；安装新版本 Python 不会自动升级已有虚拟环境。激活后可用 `python --version` 确认版本为 3.13.x。

首次启动会创建 `~/.helperme/config.json`。按[模型配置指南](docs/模型配置.md)填写模型接口和工作区后重新启动，完整配置结构见 [config.example.json](config.example.json)。

普通 Web 入口是 `python web_chat.py`，浏览器打开 `http://127.0.0.1:8765`。后端同时提供 API 和已构建的页面。`--port` 可以换端口。

Web 新建工作区时可点击「选择文件夹」，选完自动填入目录；名称未填写时使用文件夹名。目录选择窗口打开在运行 HelperMe 的本机桌面，需要 Python 的 Tcl/Tk 组件；部分 Linux 发行版需要另行安装该组件。也可以直接填写路径。

数据默认放在 `~/.helperme`。设置 `HELPERME_HOME` 会整体改写这个位置，配置和全部会话数据一起搬走。同一台机器要并行跑多个实例时，每个实例必须有自己的数据根和端口，否则会共用同一份 Journal；用 HelperMe 开发 HelperMe 的完整做法见[自举开发](docs/自举开发.md)。

前端需要 Node.js。第一次使用或改过 `web/` 之后，先构建再启动：

```powershell
cd web
npm install
npm run build
cd ..
python web_chat.py
```

开发前端时执行一个命令同时启动后端和 Vite：

```powershell
    python web_chat.py --dev
```

浏览器打开 Vite 输出的地址（通常是 `http://localhost:5173`），修改前端源码后页面会自动更新。`Ctrl+C` 会同时停止后端和 Vite。


## 工作区文件版本

Agent 每推进一步都会记录一次工作区的文件状态，所以可以让它撤回自己刚才的改动——直接说「退回到改 X 之前」，没有按钮。

版本存在 `~/.helperme` 下的独立对象库里：**不碰你的 Git 历史、分支、索引和暂存**，工作区也不必是 Git 仓库。

记录范围认两处忽略规则：工作区里的 `.gitignore`（它只是个文本文件，目录不是 Git 仓库也一样生效），以及仓库自带的 `.git/info/exclude`。**你的全局 gitignore 不参与**——为保证快照与磁盘字节一致，全局和系统 Git 配置被整体关闭了。因此平时靠全局规则忽略掉的文件会被记录进 `~/.helperme`，回退时也会被一起改动；不想要就把它们写进工作区的 `.gitignore` 或 `.git/info/exclude`。

没有任何忽略规则的大目录会被整个记下来，版本数据目前也只增不减，暂时没有回收。

## 文档

- [文档索引](docs/README.md)
- [项目架构方向](docs/项目架构方向.md)
- [架构总览](docs/架构/总览.md)
- [Runtime](docs/架构/运行/Runtime.md)
