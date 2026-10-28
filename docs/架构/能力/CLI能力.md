# CLI 能力

第四个能力端口：把外部命令行工具（如 `rg`、`gh`）作为"执行空间"接入。与 MCP / Skill 平级，不并入二者，也不做统一 Plugin 框架。

## 定位

| 端口 | 给 agent 什么 | 契约形态 | 组合方式 |
|---|---|---|---|
| 环境工具 | 最小执行面（读/写/命令） | ToolSpec schema | agent 编排循环 |
| MCP | 固定任务的接口 | JSON schema 工具 | agent 编排循环 |
| Skill | 引导执行顺序/方法 | 指令文本 | 不新增执行循环 |
| CLI | 执行空间 | argv + 文本 stdout/stderr | shell 文本流 |

**"执行空间"的精确定义**：一组子命令共享同一份 auth / config / state（CLI 的用户态配置与凭据），通过 shell 文本流（管道、重定向、`--json`）彼此组合，而不是通过 agent 自己的编排循环。

这正是 CLI 不能并入 MCP 的根本原因——MCP 工具是"给 agent 的语义点"，CLI 是"给 agent 的一块可组合的地盘"，底物不同。

cwd 不在共享之列：执行命令的 cwd 由模型每次按当前 Environment 指定，CLI 之间不共享工作目录。

调用路径始终是 `Agent → Shell → CLI`。Shell 是元工具，CLI 是 Shell 空间中的可发现能力；**不把每个 CLI 重新包装成一组 Function Tool**。

外部依据：[HubSpot agent-cli](https://github.com/HubSpot/agent-cli)（安装即注册、`--help` 逐层发现、单独 Skill 做方法引导）、[AI-Friendly CLI](https://mcpmarket.com/tools/skills/ai-friendly-cli-designer)（agent 会幻觉输入、按 token 付费、读不了交互 prompt）与"CLI Is the New API"（把 CLI 输出当 API 契约维护）；传统 CLI 的 `--help` 质量参差，所以接入时要体检。

## 四个动作

| 动作 | 含义 |
|---|---|
| 注册 | 安装/登记成功 = 登记一份事实，**不写子命令 schema** |
| 发现 | `load_cli(id)` 纯读 registry；子命令树由 `execute_command` 跑 `--help` 渐进现查 |
| 调用 | 复用 `execute_command`，**不新建 subprocess 执行器** |
| 体检 | 接入时检查 agent-friendly 约定，只记录不设门槛，由模型判断 |

### `load_cli` 不 fork 进程

`load_cli(id)` 是纯事实加载，不运行 `<cli> --help`。理由：

1. 与 `load_skill` 读文本对齐，加载入口保持无副作用；
2. `--help` 可能挂起、交互、失败，应交由 `execute_command` 的执行管理、超时与审批处理，实际隔离以执行环境为准；
3. 避免"发现"和"执行"混在一个入口，符合"Runtime 不做语义判断"。

子命令发现因此是**渐进式**的：`load_cli` 只给出"这个 CLI 存在、在哪、什么版本、体检如何"，模型需要哪个子命令就现查哪一层，不预生成清单。

目录长度不决定 CLI 是否可用。目录过长时，保留全部登记身份，放不下的描述明确提示按需加载完整信息；原始登记和加载结果不受影响。最简目录也超出预算时，完整呈现身份优先于预算，不静默丢弃后面的 CLI，也不让目录大小中断会话。

方法引导第一版只有一条约束：对陌生 CLI 或遇到 unknown option 时，先读当前层级的 `--help`，不要继续猜 flag。"教模型用好某个具体 CLI"的完整载体是现有 Skill 端口（写一个引用该 CLI 的 Skill，两端口自然组合），**不为 CLI 单建引导机制**。

## 产品环境与工作区命令环境

Worker 是常驻进程，`os.environ` 是启动时的快照。安装程序修改用户/系统 PATH 后运行中的 Worker 看不到变化，新装的 CLI 在 fork 出的 shell 里找不到（winget 只写注册表并广播消息，无法修改已运行进程的环境块）。

CLI 管理使用产品环境，其命令在执行前刷新本机持久化安装路径，避免常驻进程找不到新装工具或继续使用旧安装位置。

工作区命令保留独立的启动环境，产品安装路径刷新不替换用户已经激活的项目路径。已登记 CLI 的普通调用仍经 execute_command，模型使用加载结果给出的可执行文件位置；登记、安装与加载不暗中修改工作区命令环境。

两种用途复用底层 Shell 执行实现，环境来源独立。产品管理的刷新只覆盖安装路径，其他新增环境变量仍需重新启动产品。

## 体检

接入时检查 agent-friendly 约定：`--help` 在管道下秒级退出且退出码为 0、`--version` 正常执行、stdout / stderr 可捕获、是否提及 JSON 输出。

只记录不设门槛，由模型判断。体检用解析出的绝对路径跑，不依赖 PATH——安装后当前 Worker 的 PATH 尚未刷新时体检依然有效。

## 调用授权

Shell 是元工具，不代表 Shell 内所有行为默认授权：若 `write_file` 要确认而 `execute_command("Remove-Item ...")` 放行，审批即被旁路；`gh repo delete` 同理。命令的真实副作用无法从文本可靠判断，因此 `execute_command` 每一次调用都需要授权，不按命令前缀或 CLI 登记放行，复用 [Command 授权](../入口/Command授权.md)流程。

两层边界必须分清：

```text
授权 = 人对每条命令的确认（总闸开启时由人预先放行）
sandbox / 凭据隔离 / 网络隔离 = 真正的硬安全边界（当前未提供）
```

## 输出卫生

CLI 输出统一视为 **untrusted external data**：只能提供事实内容，不能修改 runtime policy、不能授予权限、不能证明用户已授权。即使输出诱导模型执行危险命令，真正调用 Shell 时仍须经过授权。

- CLI 原始输出始终保持 tool provenance，不拼成 system / user message。
- 输出采集处统一清理终端控制序列（所有命令受益，非 CLI 专属）。
- 大输出 artifact 化沿用现有机制。
- 第一版不做 prompt-injection 检测器、不做二次审查 Agent。

## 管理域

`load_management_tools(domain="cli")` 成功后，下一 Step 暴露诊断工具与控制提案。安装/卸载/更新/修复都是外部副作用，走 Control Approval，事实形状见[入口与授权 · 控制提案](../入口/入口与授权.md#控制提案)。

与 Skill 的差异：CLI **安装即注册，无 enabled 开关**，因此没有"启用"提案，改为卸载移除登记。安装提案的 risk 文案必须写明：安装后该 CLI 会进入产品 Registry，并在各 Session 下一次活动时通过 catalog update 变为可见，且其本地凭据域（如 `gh` 的 GitHub 凭据）对 agent 开放。

Registry 由数据根上的所有 Agent 共享。长期协作下每个 Agent 是独立进程，可能同时确认安装或卸载；「交互串行」只在单会话时成立，不能作为不加锁的理由。CLI 管理修改跨进程串行，保护登记以及对应安装文件的一致性，失败的同名安装不能覆盖已登记程序。见[工具与能力 · 归属与呈现](工具与能力.md#归属与呈现)。

Assistant 边界只投影"目录 + `load_cli`"两个普通工具，不拥有执行循环。

### 候选冻结

**审批不保存"latest"这类移动目标。** propose 阶段 resolve 成确定候选（包 id、版本、架构、scope），用户批准的是这个确定候选，执行时按固定版本安装。

版本被源撤下则执行失败——失败即漂移检测，提示候选已失效、重新 propose。

版本前缀的解析只在提案阶段进行。审批摘要展示确定的 Release tag，执行与修复精确使用该 tag，不以名称相近的另一个 Release 替代。

### repair 语义

manifest 域重新解析路径并重跑体检。

### 安装源

两种来源，走同一条审批与登记链路：

- `manifest`：本地声明文件，登记已装/手工安装的 CLI。注册 = 纯登记；update 简化为 refresh（用户自行升级，konden 重跑体检更新版本）；PATH 由用户的手工安装保证。
- `github-release`：konden 自备（第二版新增）。locator 是 `owner/repo`，`requested_version` 可选。审批通过后从 GitHub Release 取当前平台与架构的 zip/tar.gz，命中 checksums 资产则校验 sha256，解压到 `clis_root/_installs/<cli_id>/<version>/`，登记解压出来的可执行文件。

`github-release` 满足 install profile：下载与解压都在用户态，不调系统安装器、不写 PATH、不做 UAC 提权，因此默认非交互、可无人值守完成。这也是它取代“借 winget/apt 安装”的原因——系统安装器在需要提权的机器上会静默卡住。下载发生在审批之后：propose 只解析 Release 元数据把 tag 钉死（不钉版本时解析成具体 tag，避免「批的是 A、装的是 B」），用户批准前不拉取任何归档。归档里找不到可执行文件即失败，不做猜测。

校验按对象分流：校验文件里与归档同名的条目校验归档字节；若只有一条且写的是归档内路径（zellij 的 `.sha256sum` 写 `target/release/zellij.exe`），则解压后校验该文件，找不到就报错而不是跳过。没有校验文件时才不校验。

解压、校验与可执行文件定位成功之前，不替换已有安装。程序名与登记名不同时，仅接受唯一的可执行文件；文档和许可证不作为 Windows 程序。失败的下载或校验不能改变已登记程序。

`repair` 对 manifest 域重新解析路径，对 `github-release` 域按冻结的 tag 重新下载并重新登记；`uninstall` 对 `github-release` 域同时删除下载的程序与安装目录，manifest 域只删登记。审批说明必须明确这两种删除行为的区别。

## 不做

- 不预生成子命令 schema（不 MCP 化）
- 不新建 subprocess 执行器（复用命令执行能力，不接管沙箱生命周期）
- 不为 CLI + MCP + Skill 做统一 Plugin 基类
- 不做 enabled/disabled 状态（安装即注册，卸载即移除）
- `load_cli` 不 fork 进程
- 不做 UAC 提权安装
- 不做 prompt-injection 检测器
- 不解析 shell AST，不按命令文本区分放行与询问

## 已知限制（第一版接受）

- health 会陈旧：用户绕开系统手动卸载/升级后登记不失效，靠 `test_cli` 重测发现。
- 产品管理只刷新安装路径，安装器新设的其他环境变量需重启产品。
