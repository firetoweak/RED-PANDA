# LiteLLM 接入

HelperMe 使用进程内 LiteLLM Router 作为模型适配层。LiteLLM 负责 Provider 配置、协议转换和未来可选的路由能力；Assistant 继续只依赖窄 `LLMApi`，Runtime、Journal 与 `ModelDecision` 不认识 LiteLLM 或厂商字段。

```text
Assistant → LLMApi → Worker LLM Port → Host LLM Service → LiteLLMAdapter → litellm.Router → Provider
```

Router 由 Host 持有、跨 Session 复用；**Worker 禁止 import litellm**。附件编码、流式预览身份和 `cancel_turn` 仍留在 Worker。

本次接入只解决已经发生的协议差异，不引入 LiteLLM Proxy，不接管 HelperMe 的 Agent 循环、工具执行、上下文投影或持久化。可复制的配置样例见[模型配置](../../模型配置.md)。

## 配置所有权

HelperMe 只校验模型配置的**外层形状**：当前逻辑模型名与 Router 配置必须存在。Router 配置内部不枚举模型、Provider 或参数字段，原样交给 LiteLLM 校验和解释。

这是传给 Python SDK 的数据形状，不复用 LiteLLM Proxy 的配置加载器，也不承诺 Proxy 专属的 include、密钥引用或热更新语义。

LiteLLM 必须在进程内以离线静态资料初始化：Adapter 在首次导入前强制使用随包安装的模型资料表，并清除会覆盖随包 tokenizer 目录的外部缓存路径。缺失的安装资源应直接暴露为初始化失败，不允许启动时联网下载补齐。因此这项策略属于接入不变量，不进入用户配置。

Compact 阈值由 HelperMe 配置。第一阶段一个逻辑模型只对应一个部署，不配置 fallback、缓存或跨模型负载均衡。重试次数与单次尝试超时不写进用户配置，由代码默认注入 Router。

## LLMApi 契约

Adapter 把 LiteLLM 响应收成两部分：正文、工具调用与 usage 转换为已有的归一化结果；assistant message 中其余非空字段转换为普通 JSON `message_extensions`。

`message_extensions` 是**模型协议附件，不是对话事实**，也不是 `ModelDecision` 的组成部分。它不得包含由 HelperMe 拥有的 `role`、`content`、`tool_calls`。空字符串是实际值必须保留；`null` 与不存在的可选字段不写入。

LiteLLM 对象、Pydantic 类型和异常类型不得越过 `helperme/llm`。

## 流式调用

Adapter 默认使用流式调用，**不保留另一条非流式主链路**。调用方可传入正文增量与思考增量的观察者，两者分开，不混入彼此；不需要展示的调用（Compact、Skill 摘要等）不传观察者。

所有 chunk 仍由 LiteLLM 组装为一次完整响应，再进入现有校验。HelperMe 不自行拼接工具调用、推理字段或 Provider 扩展。已交给观察者的正文增量拼接后必须与最终响应完全一致，否则整次响应无效。

正文与思考增量都是调用期间的可丢弃观察结果，不是 Event、不是 ModelDecision、不是第二事实源。Runtime API、Event 和状态投影均不感知流式展示。

## 持久化与重放

一次模型响应提交为 Step 时，非空 `message_extensions` 写入 Step 的决策元数据，与 LoopGuard 的 metadata 显式组合。Runtime 只把它当不透明 JSON 原子持久化。

投影时用扩展字段 + 从 ModelDecision 重建的正文 + 从 Commands 重建的工具调用，组装出 assistant 协议消息。扩展字段不能覆盖核心字段。

**不能把原始 assistant message 整包当成下一轮历史**：原始 tool call id 不是 Runtime 提交后的 command id，控制命令和 deliver 也可能改变最终可见投影。

扩展字段在模型协议投影时恢复，**发生在预算评估之前**。因此它们参与实际输入预算、Compact 和窗口继承；不可在调用前临时补入，否则预算会失真，Compact 后也会丢失。

只有当前 Context Window 可见且确实投影出 assistant message 的 Step 才携带扩展字段，不另建推理历史规则。

## 异常与生命周期

Adapter 只转换 LiteLLM 明确定义且 HelperMe 已有语义对应的异常：认证与权限、上下文超限、连接/超时/限流/服务端暂态错误及其他已知 Provider 错误。无确定对应关系的异常原样暴露，不以"模型不可用"或空响应继续运行。

跨进程时这些异常收成 HelperMe 类型或带原始类型名与 traceback 的穿透异常，Worker 不反序列化 LiteLLM 类型。**Provider 错误不得升级为 Host 进程失败。**

Router 随 Host 生命周期创建和关闭。配置错误在装配边界暴露，不补默认 Provider、不猜测模型名、不降级到其他模型。Worker 退出或 `cancel_turn` 只中止对应调用，不关闭 Router。

## 版本与路由演进

LiteLLM 使用精确版本，不用开放上界自动升级。每次升级单独验证，不把新版功能自动接入。

未来启用 LiteLLM 已有的权重、顺序 fallback 或负载均衡时，原则上只修改透传给 Router 的配置。但启用前必须先解决两件事：

- 决策证据能够记录一次调用实际命中的公开模型/部署身份；
- 同一逻辑模型下不同部署的上下文窗口不会使请求前预算失去确定依据。

需要 HelperMe 根据任务语义选择模型、传递 Session 路由标签或改变上下文策略时，属于新的产品设计，不伪装成纯配置更新。
