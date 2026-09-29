# Channel 接入契约

Channel 把外部通信协议映射到 Assistant 的 Session 操作。它不实现模型决策或 Session 推进循环，也不决定本机用哪套环境变量去找程序、跑命令。

TUI 的具体行为见[入口与授权](入口与授权.md)；进程驻留和进程身份见[多活跃会话](../运行/多活跃会话.md)；四个 Channel 的平级关系见 [Channel 协议改造](Channel协议改造.md)。

## 四种 identity

| identity | 用途 |
|---|---|
| Access | 谁可以使用入口 |
| Conversation | 稳定对话 identity，直接对应业务 Session |
| Delivery | 幂等接纳一条外部消息 |
| Reply route | 把输出送回正确会话 |

**凭证不是 Conversation identity。** IM 类入口由 Bot 账号与对话共同选择稳定 Conversation，token 只用于访问；进程重启恢复同一 Session，更换 Bot 不复用旧 Session。

每个 Channel 实例还提供稳定 owner identity，通过 `select` / `release` 声明用户当前使用的 Session。这是 Host 的瞬时驻留事实，不写入 Journal，Host 重启后由 Channel 重新选择。**owner 只控制驻留与回复路由，不是 Command 授权身份**；一个 Session 同时存在多个 owner 时也不改变其授权策略。

[Compact](../上下文/Compact.md) 只切换同一业务 Session 内的上下文窗口，Channel 使用稳定 Session identity，不为压缩维护入口路由。

## 输入

Channel 把原始文本交给一次 `accept_input()` 请求，Assistant 在同一个 Worker 内按固定优先级处理：control confirmation、Command authorization、terminal、普通用户消息。

**这个顺序在 Worker 内一次决定，是为了消除 `view → action` 竞态**——Channel 不先查状态再决定发什么。所有入口共用同一行为。

所有普通文本，无论 Session 当时正在模型决策、执行 Command 还是等待输入，都单次接纳为 `UserMessageReceived`：

```text
外部消息
→ Host 解析当前 Session 并登记 Delivery 归属
→ accept_delivery(source, delivery_id)
→ UserMessageReceived
→ wake(session_id)
```

Channel 不区分 running/idle 输入，不创建 Interrupt 类型，不抢占当前 Step，也不等待一次用户消息对应的"Run"完成。接纳即时，当前 Step 使用冻结视图。后到消息如何改写下一拍，由 Runtime 归约，见 [Runtime 的"用户输入与后到消息"](../运行/Runtime.md#用户输入与后到消息)。

明确的授权 `yes/no` 由 Assistant 应用边界映射为授权事实，control confirmation 优先于 Command authorization。其他文本一律保留为用户消息，Runtime 不猜语义。

待裁决的控制提案从 Journal 投影，Worker 重启后仍在。裁决只回答是否允许，不夹带执行结果；过期或冲突的裁决作为应用边界的明确冲突返回，不落成第二条裁决。

## 输出

模型生成期间，Assistant 可以向 Channel 发 `started / delta / aborted` 三种 preview 信号。**preview 只用于当前连接的展示**：不写 Journal，不参与重放，也不表示正文已经提交。`started` 只建立一次输出的展示身份，没有正文 delta 时不应画出空消息。

一次 preview 使用当前 trigger event id 作为稳定 `output_id`。模型调用、响应校验或 Step 提交失败时发 `aborted`，不新增 assistant 正文事实。Worker 来不及发送就异常退出时，Host 按 Session、Worker generation 和活动 `output_id` 发同义的清理信号。已经显示的部分正文由 Channel 标成中止，**不能提升为事实**。

Step 原子提交后，Assistant 正文通过产品拥有的 `deliver` Command 到达 Channel sink，用同一 `output_id` 与 preview 对齐，不重复显示；两者不一致属于内部契约违规。

投递失败不回滚 Step，也不重新调用模型或业务工具；重试和平台能提供的幂等程度由 Channel 承担。

控制提案本身不走 `deliver`，不进对话时间线。裁决和执行说明的内容只能从 Journal 中同一 `request_id` 的事实投影，不由 Application 维护第二份结果。见[入口与授权 · 控制提案](入口与授权.md#控制提案)。

## Session 操作

- `/new`：生成新 identity，创建 Session，并将当前 Channel owner 原子切换到它；
- `/resume <session_id>`：切换到已存在的 Session、完成未发布的窗口切换准备、重建 Host 投影，并按当前 State 决定是否 wake；切换失败时保留原选择；
- 不提供 `/stop`；退出进程不写 Runtime Event。

Channel 关闭时释放 owner。Runtime 的 `WAITING` 不决定 Worker 是否退出——Host 在 Worker 静止后根据 owner 选择决定是否继续驻留。

一次回答结束后 Session 回到 `WAITING(external_fact)`，后续文本进入同一 Session。compact 可以在两次决策之间切换 ContextWindow，Session、用户入口和 Reply route 不变。Session 没有绝对终态。
