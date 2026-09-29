# Channel 协议改造

四个 Channel 的架构关系与职责边界。当前接入行为见 [Channel 接入契约](Channel接入契约.md)，Web 已落地见 [Web](Web.md)。

## 目标

TUI、Web、ACP、Satori 四个 Channel 平级并存，共享 Assistant 应用能力，**不以 ACP 统一所有入口**。

| Channel | 职责 | 当前安排 |
|---|---|---|
| TUI | 自有终端交互，原生使用 Assistant 应用能力 | 保留独立入口，不迁入 ACP |
| Web | 自有网页交互，原生使用 Assistant 应用能力 | 已落地 |
| ACP | 适配 Obsidian 等外部编辑器与 ACP Client | 保留已有实现与边界约定 |
| Satori | 适配 IM 平台 | 保留架构方向，当前暂缓 |

```text
TUI ─────┐
Web ─────┤
ACP ─────┼── Assistant 应用操作 / 查询 ── Host / Worker ── Runtime / Journal
Satori ──┘   （Satori 暂缓）
```

**平级不要求功能完全一致。** TUI 与 Web 可以按产品需要呈现事件级历史、消息分叉等原生能力；ACP 的协议表达范围不构成其他入口的能力上限。

## 职责边界

各 Channel 负责交互、外部协议适配、identity、投递和回复；Assistant 负责模型决策、授权应用操作、Session 装配与面向入口的查询投影；Runtime 负责事实持久化、确定性归约和执行不变量，**不出现 TUI、Web、ACP 或 Satori 名词**。

共享能力沿协议无关的应用端口扩展。Channel 不直接操作 Journal、不实现 Runtime 推进循环、不维护第二份会话事实；展示从 Journal / Assistant 事实投影，各入口自行适配。

不为四个入口增加宽泛的 Channel 基类、统一消息模型或能力协商框架。入口之间不互相转译，不要求连接生命周期与配置 Schema 相同。

TUI 原生接入是正式路径，与 ACP 平级并存**不属于兼容双轨**。IM 平台只经 Satori 接入，暂缓期间不保留任何单平台 IM 入口；原有 Telegram 入口已删除，恢复 Satori 专题时从零设计，不以它为迁移起点。

## 共同约束

- Session 与 Event 仍以 Journal 为唯一事实源，前端聊天记录不承担恢复执行的职责。
- Channel 使用 Assistant 应用边界，不绕过 Workspace、授权与能力渐进加载规则。
- Host 按本机用户环境拉起 Worker，不使用外部编辑器残留的执行环境。
- 各入口只承诺自身已经实现的能力，不为其他入口保留无实际用途的适配路径。

## ACP 的取舍

以下只适用于 ACP 独立入口，不作为 TUI / Web 的共同协议。

### 锁定 wire protocol v1

目标客户端是 Obsidian Agent Client，其发布版锁定的 SDK 初始化请求使用协议版本 1。因此 HelperMe 的 ACP 实现以 **v1 为唯一目标**，不按仍在演进的 v2 Prompt 生命周期实现。

ACP 没有 HelperMe 的 Step，也没有「步骤上下文」方法。一次 prompt 请求挂到 Session 静止为止，中间多个 Step 都落在这一轮里——这只是协议等待与展示投影，**不把「ACP Turn」写成新的 Runtime 状态**。

### identity 映射

| HelperMe identity | ACP 来源 | 规则 |
|---|---|---|
| Access | 本地 Agent 进程的启动者 | 只支持本地 stdio。操作系统用户跟启动者走；干活用的环境变量不是 Client 进程自带的那份，见[多活跃会话 · 进程身份](../运行/多活跃会话.md#进程身份)。客户端声明只作诊断，不作授权依据 |
| Conversation | `sessionId` | 由 HelperMe 生成并持久创建 |
| Delivery | 一次已接纳的 prompt | ACP 没有跨连接稳定的投递标识；Adapter 在接纳边界生成唯一 ID，支持本地 stdio 的单次请求，不宣称崩溃后重发幂等 |
| Reply route | 连接 + `sessionId` | 仅为瞬时路由，重连后由新连接重新选择，不写成 Runtime 事实 |
| Owner | 连接内的会话绑定 | 一条连接可以选择多个 Session，关闭时逐一释放 |

**不能用 Prompt 文本哈希充当 Delivery identity**：用户连续发送相同文本是两个真实事实。也不能把 JSON-RPC request id 宣称为跨连接稳定 identity，协议没有给出这项保证。

以后若支持网络 ACP 或自动重试客户端，必须先取得协议级稳定投递标识，或明确增加双方协商的扩展；不能用内容去重、时间窗或进程内缓存伪造可靠性。

### 接了什么

初始化、新建会话、prompt、取消四个方法已映射。`cwd` 必须是已存在的绝对目录，按最深匹配复用已登记工作区，找不到则隐式登记；一条会话只绑一个工作区，不能用环境变量改执行环境。客户端注入的 MCP 配置不接受——HelperMe 继续拥有自己的 Registry 与渐进加载。

取消映射为 `cancel_turn`：终止该 Step 正在执行的 `execute_command`，不终止 Session，不映射成用户消息。

对外推送模型正文和工具调用。ACP 用量协议要求模型总容量，而产品只维护 compact 触发阈值，因此不把阈值作为总容量上报。正文 preview 与提交后的 `deliver` 共用同一输出身份，`deliver` 不重复全文，也不重复报工具。**工具终态信号只能在对应 Runtime Outcome 已经提交后发送**——执行函数返回、IPC 中断或 Worker 退出本身不能让 ACP 把 unknown 画成完成或失败。

### 明确不接，及理由

- **权限询问**：授权走 Assistant 应用边界，ACP UI 的回答本身不是 Runtime 授权。目标支持，尚未接线。
- **Client 文件系统 / 终端**：与现有 Sandbox / Tools 的所有权重叠，执行仍在本机 Sandbox。
- **历史重放（`session/load`）**：不从 Journal 向 Client 重放历史。完成最小闭环后再实现，以支持 Obsidian 恢复。
- **思考流**：没有独立思考通道，`deliver` 不改判成思考。
- **计划、斜杠命令表、Session 模式**：没有对应产品面。
- **用户原文回放**：不把用户消息回放给 Client。
- **prompt 中的图片、音频、嵌入资源**：一律拒绝。

初始化时没声明的能力，Client 不应调用。

### 展示是投影，不是事实源

```text
Journal Event / Assistant lifecycle
                 │ project
                 ▼
          ACP session/update
                 │ render
                 ▼
          Obsidian / 其他外部 ACP Client
```

连接断开可以导致展示失败，但不能回滚已经提交的 Event、Step 或 Command。恢复历史时重新从 Journal 投影，**不能依靠 ACP Client 保存的聊天记录恢复 Runtime**。

已知投影缺口：工具成功结果只在原始输出字段里，不进协议建议的内容数组；模型用 `deliver` 做逐步汇报时全部进聊天气泡，协议没有折叠面。

## 当前范围

本文不安排 TUI / Web 迁入 ACP，不推进 Satori 开发，也不以完成 ACP 闭环作为其他入口的前置条件。

消息分叉与从时间线某一步重开已在 Web 落地，历史分支与文件回退的边界见 [Web](Web.md) 和[工作区版本](../运行/工作区版本.md)。分支共享历史前缀，见[会话分支存储](../运行/会话分支存储.md)；会话分支只分会话、不分文件世界，多条会话线共用同一条工作区轨道。Satori 的协议字段、identity 映射与平台迁移方案在恢复该专题时再设计。
