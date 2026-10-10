# Command 授权

解决"工具副作用需要用户确认"的交互。与 Control Approval（批准安装、更新等管理提案）分离，**两者变化原因不同，不抽象成通用审批框架**。控制提案见[入口与授权 · 控制提案](入口与授权.md#控制提案)。

## 授权判定是纯函数

每个 Command 派发前经授权判定得出 verdict（`allow` / `ask`）。判定只看 (tool, args)，**不读外部状态**：

- 静态工具：绑定的布尔声明，即常量 verdict。用户工作区的普通写入需要确认，只读工具不需要；子的写入只落在独立工作树中，不需要确认，父的合入需要。效果边界见[子任务文件交换](../Sandbox/子任务文件交换.md)。
- `execute_command`：挂 ExecPolicy，按命令文本前缀规则动态判定，见 [CLI 能力](../能力/CLI能力.md)。

判定函数随工具 Binding 注入，Runtime 只消费 verdict，不理解规则含义。

授权是**正交的派发 gate**：Command 的执行生命周期只描述跑到哪一步，是否派发由授权事实和拒绝事实决定，两者互不代替。

## Session 总闸

`auto_authorize` 表达"这个 Session 我信任 agent，别再问"。

它是 **Session 级产品偏好，不是任务事实**，所以由 Host 持有、不进 Journal，模型无需知道。只在人拨过时写入：创建不写入口默认值，Fork 不继承，没拨过就是关。Worker 不读写这份元数据，只收 Host 推来的确定值。

**owner 选择只负责驻留和回复路由，不参与授权。** 一个 Session 可以同时有多个 owner，也可以在后台无人选择；授权策略在这些情况下必须保持相同。当前只有 Web 暴露设置入口，但语义不依赖 owner 类型——同一条 Session 从 Web 转到 TUI 或 ACP，仍使用它已经明确设置的总闸，入口切换不暗中改写授权。

verdict 为 `ask` 时：总闸开启则 Host 为该 Command 追加明确授权事实；总闸关闭则等待显式批准。TUI 的 yes/no 一次性处理当前全部待授权命令，Web 按单个 command 处理。**没有任何入口在线时也不会替人改变总闸或伪造批准。**

自动放行挂在调度器的静止钩子上，Host 同步 Session 策略时再补一次。真正允许派发的始终是 Journal 中对应 command 的授权事实。

## 状态归约

工具展示状态里只有 `running` 是可丢弃的活动叠加，其余全部来自 Journal 投影。基础状态按单个 command 归约，顺序固定：

1. 有 Outcome → 成功 / 失败
2. 出现在拒绝事件里 → 已拒绝
3. 在待授权集合里 → 等待授权
4. 已有 Attempt 且无 Outcome → unknown
5. 尚无 Attempt → 排队中

Host 另以精确 command 身份提供活动叠加：只有 Worker 明确报告该 Command 正在执行时，Channel 才把排队或 unknown 临时画成 running。**Session 级 running / idle 不参与单个 Command 的判定。**

Worker 报告执行函数已经返回也不能产生终态——终态必须等 Outcome 提交后由 Journal 投影，实时事件只负责提示 Channel 重拉。刷新后等待、拒绝和终态从 Journal 恢复；总闸不在 Journal 里，走 Host 元数据。

## 拒绝语义

用户拒绝某次工具执行，**不是**把工具从可见 schema 中移除。模型仍看得到工具，只是明确知道"这次调用被用户拒绝"，从而避免困惑、不会原样重试。

投影层为被拒绝的调用生成一条对应的 tool 消息，保证每个 tool_call 都有配对回复（Chat Completions 协议完整），并在文本里明确这是**用户拒绝**而非工具失败或超时——两者会导向完全不同的下一步。

## 设计决策

- 总闸是 Session 产品偏好，不是任务事实，所以不进 Journal；由哪个入口设置不改变其作用域。
- 创建时不把入口默认值落盘。
- TUI 从"全放行"改为"拦截 ask"：ExecPolicy 是防误操作的软边界，误操作在任何入口都是误操作，而 TUI 已有 yes/no 交互，机制无需新增。注意此变化使写文件类工具在 TUI 也开始拦截。
- 被拦命令的完整参数（含 `execute_command` 的命令文本）要出示给用户，否则他确认的是一个看不见的对象。命中了哪条规则暂不展示——等"看不清为何被拦"成为真实问题再加。
