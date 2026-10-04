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

设计依据来自三处外部实践：HubSpot agent-cli 在生产验证了"安装即注册 + `--help` 逐层发现 + 单独 Skill 做方法引导"的组合；AI-Friendly CLI 8 原则指出 agent 会幻觉输入、按 token 付费、读不了交互 prompt；"CLI Is the New API"主张接入后要把 CLI 输出当 API 契约维护。传统 CLI 的 `--help` 质量参差，这正是体检存在的原因。

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
2. `--help` 可能挂起、交互、失败，应交由 `execute_command` 的沙箱 + 超时 + 审批兜底；
3. 避免"发现"和"执行"混在一个入口，符合"Runtime 不做语义判断"。

子命令发现因此是**渐进式**的：`load_cli` 只给出"这个 CLI 存在、在哪、什么版本、体检如何"，模型需要哪个子命令就现查哪一层，不预生成清单。

方法引导第一版只有一条约束：对陌生 CLI 或遇到 unknown option 时，先读当前层级的 `--help`，不要继续猜 flag。"教模型用好某个具体 CLI"的完整载体是现有 Skill 端口（写一个引用该 CLI 的 Skill，两端口自然组合），**不为 CLI 单建引导机制**。

## 执行环境：每次 spawn 重算 PATH

Worker 是常驻进程，`os.environ` 是启动时的快照。安装程序修改用户/系统 PATH 后运行中的 Worker 看不到变化，新装的 CLI 在 fork 出的 shell 里找不到（winget 只写注册表并广播消息，无法修改已运行进程的环境块）。

因此 PATH 不作为 Worker 生命周期内的固定状态，而在每次 spawn 子进程时动态合成：

```text
child_env =
    Worker 环境快照
    + PATH 键整体替换为最新 Machine PATH + User PATH 拼接
    + RED PANDA overlay（禁用颜色、分页与交互行为）
```

**整体替换，不是合并追加**：旧 PATH 条目若继续排前面，CLI 升级换安装位置后会被旧路径遮蔽。其余环境变量键保留 Worker 快照。

任意 Worker 下一次执行 shell 自然看到新装 CLI，不重启 Worker，不跨进程同步。落点在 sandbox 的进程执行处，所有 fork 统一经过，不是 CLI 专属逻辑。

已知限制：只刷新 PATH，安装器新设的其他变量仍需重启 Worker 才可见。Unix 上包管理器一般装进已在 PATH 的目录，此适配点近似 no-op。

## 执行器：同一 ProcessRunner + 执行 profile

不拆两套执行器实现。底层同一个 ProcessRunner，按用途分 profile：normal（普通超时、非交互，agent 日常命令）与 install（长超时、显式非交互参数，包管理器安装）。

install profile 的 ProcessRunner 是**进程级单例**，装配时注入 CLI Application，不经过 per-Session 的 EnvironmentBinding——包管理器安装与 workspace 无关。依赖方向是 `redpanda/cli → redpanda/sandbox`。

需要管理员提权的包明确失败并提示手动安装：UAC 弹窗会使子进程进入另一安全上下文，输出采集断裂，不做。

## 体检

接入时检查 agent-friendly 约定：`--help` 在管道下秒级退出且退出码为 0、`--version` 正常执行、stdout / stderr 可捕获、是否提及 JSON 输出。

只记录不设门槛，由模型判断。体检用解析出的绝对路径跑，不依赖 PATH——安装后当前 Worker 的 PATH 尚未刷新时体检依然有效。

## 调用授权：ExecPolicy

Shell 是元工具，不代表 Shell 内所有行为默认授权：`write_file` 要确认而 `execute_command("Remove-Item ...")` 放行，审批即被旁路；`gh repo delete` 同理。

因此在 `execute_command` 上挂 ExecPolicy，按命令文本的前缀规则判定 verdict，第一版只有 `allow`（默认）与 `ask`（内置危险前缀清单）两档，后者复用 [Command 授权](../入口/Command授权.md)流程。

两层边界必须分清：

```text
ExecPolicy / 授权 = 防止误操作的软边界（可被变量、alias、脚本绕过，不追求覆盖率）
sandbox / 凭据隔离 / 网络隔离 = 真正的硬安全边界
```

ExecPolicy 不解析 shell AST，不试图理解所有 CLI 的真实副作用；规则匹配是纯字符串判定，入口无关。它不属于 CLI 包——它是 `execute_command` 的授权判定，判定函数随工具 Binding 注入，Runtime 只消费 verdict。

## 输出卫生

CLI 输出统一视为 **untrusted external data**：只能提供事实内容，不能修改 runtime policy、不能授予权限、不能证明用户已授权。即使输出诱导模型执行危险命令，真正调用 Shell 时仍经 ExecPolicy。

- CLI 原始输出始终保持 tool provenance，不拼成 system / user message。
- 输出采集处统一清理终端控制序列（所有命令受益，非 CLI 专属）。
- 大输出 artifact 化沿用现有机制。
- 第一版不做 prompt-injection 检测器、不做二次审查 Agent。

## 管理域

`load_management_tools(domain="cli")` 成功后，下一 Step 暴露诊断工具与控制提案。安装/卸载/更新/修复都是外部副作用，走 Control Approval，事实形状见[入口与授权 · 控制提案](../入口/入口与授权.md#控制提案)。

与 Skill 的差异：CLI **安装即注册，无 enabled 开关**，因此没有"启用"提案，改为卸载移除登记。安装提案的 risk 文案必须写明：安装后该 CLI 会进入产品 Registry，并在各 Session 下一次活动时通过 catalog update 变为可见，且其本地凭据域（如 `gh` 的 GitHub 凭据）对 agent 开放。

Assistant 边界只投影"目录 + `load_cli`"两个普通工具，不拥有执行循环。

### 候选冻结

**审批不保存"latest"这类移动目标。** propose 阶段 resolve 成确定候选（包 id、版本、架构、scope），用户批准的是这个确定候选，执行时按固定版本安装。

版本被源撤下则执行失败——失败即漂移检测，提示候选已失效、重新 propose。

### repair 语义

manifest 域重新解析路径并重跑体检。包管理器域按登记的版本固定重装（与 update 的"移动"区分）；版本为空则拒绝 repair，提示走卸载 + 安装。

### 安装源

第一版只有 manifest：本地声明文件，登记已装/手工安装的 CLI。注册 = 纯登记；update 简化为 refresh（用户自行升级，RED PANDA 重跑体检更新版本）；PATH 由用户的手工安装保证。winget 作为第二切片，带着 install profile 与候选冻结一起来。

## 不做

- 不预生成子命令 schema（不 MCP 化）
- 不新建 subprocess 执行器（同一 ProcessRunner + profile）
- 不为 CLI + MCP + Skill 做统一 Plugin 基类
- 不做 enabled/disabled 状态（安装即注册，卸载即移除）
- `load_cli` 不 fork 进程
- 不做 UAC 提权安装
- 不做 prompt-injection 检测器
- ExecPolicy 不解析 shell AST；verdict 不做 deny 档

## 已知限制（第一版接受）

- registry 多进程并发写无文件锁：两个 Worker 同时确认安装可能后写覆盖先写；Control Approval 交互串行使概率极低。
- health 会陈旧：用户绕开系统手动卸载/升级后登记不失效，靠 `test_cli` 重测发现。
- 只刷新 PATH，安装器新设的其他环境变量需重启 Worker。
- install 是分钟级长操作，其执行期间该 Session 的控制面交互语义在实现时明确。
