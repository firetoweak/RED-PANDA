# 模型 Provider 接入

HelperMe 只拥有模型调用的窄协议 `LLMApi`。Host 按 Provider 持有 OpenAI-compatible Chat Completions 客户端，供选择该 Provider 的 Session 复用，直接连接所选 Provider；不引入外部网关进程，不在进程内加载模型 SDK 或路由器。

```text
Assistant → LLMApi → Worker LLM Port → Host LLM Client → Provider
```

Worker 通过 Host LLM port 调用，不能直接连接 Provider。

## thinllm 与 HelperMe 的分界

Provider 表、流式客户端、调用结果类型和 LLM 错误类型放在与 `helperme` 平行的独立包 `thinllm` 中。`thinllm` 不 import `helperme`，由 `tests/architecture/` 守住；它只回答「给定 `provider/model` 与一份连接设置，怎样发出一次流式调用」。

HelperMe 保留 `LLMApi` 协议、Worker LLM Port 与 IPC 编码、图片附件编码、个人连接设置加载以及面向用户的失败文案。拆分是为了让 Provider 层的膨胀有明确的边界，不是为了单独发布；`thinllm` 不做成通用网关。

## 模型标识与 Provider 表

模型标识写作 `provider/model`，按第一个 `/` 拆分：前半段选 Provider，后半段原样作为上游模型名。选模型就是选 Provider，HelperMe 不做路由。

Provider 是项目内置的一张表，不是用户配置。每项只描述接口地址来源、认证要求和该 Provider 已知的协议差异。远程接口地址是项目事实；本地部署地址与凭据从个人数据目录的独立结构化连接文件读取，与用户模型偏好分开。Web 可在连接未配置时启动，选择和调用边界明确报告缺失的必要设置。新增 Provider 意味着加一项表项与对应测试，不开放配置自定义。

## 连接生命周期

连接设置在每次调用边界读取。相同 Provider 与相同连接设置复用客户端；设置变化后，新调用使用新客户端，在途请求保留自己的旧客户端，最后一个请求结束后才释放旧连接。文件损坏或内部错误直接暴露，不以旧连接掩盖失败，也不要求重启应用。

会话的模型与生效边界见[会话模型选择](../入口/会话模型选择.md)。后台 compact 使用启动该任务时捕获的模型，已有任务不会因用户切换而被改写。

## 协议转换

客户端只负责 HelperMe 调用契约与 HTTP/SSE 协议之间的转换：内容增量和推理增量分开投递；工具调用分块重组为完整调用；usage 与模型消息扩展进入现有归一化结果。图片附件在 Worker 请求边界编码。工具执行、上下文投影、持久化与 Agent 循环始终归 HelperMe 所有。

Provider 协议差异随表项声明，只对该 Provider 生效，并且只依据已经遇到的真实协议要求添加。例如 DeepSeek 要求回放的 assistant 消息必须带推理字段；模型某轮未产出推理时，客户端只在发出请求时补空推理字段，Journal 仍如实记录该轮没有推理。

## 失败与重试

适配器只把明确识别的认证、上下文超限、暂态传输和服务错误转换为 HelperMe 的 LLM 错误类型。未识别的内部异常原样暴露；无效 HTTP/SSE 响应作为无效模型响应处理。HelperMe 不猜测错误文案含义。

暂态失败只在响应流尚未收到任何字节时有限次退避重试。流一旦开始，内容增量可能已经投递给预览，此后的失败原样上报，由既有的 Step 失败路径处理。重试是确定性的连接机制，不涉及语义判断。

## 明确不做

- 不做跨 Provider fallback、负载均衡、熔断、计费、模型目录或自动发现。
- 不支持在配置中自定义 Provider；不为协议不同的模型预建抽象，届时单独适配。
- 不保留 Ferro、LiteLLM、进程内 Router、网关启动命令或兼容路径。
- 不把模型流式预览写入 Journal；只有完整、有效的模型结果参与 Step 提交。

需要复杂路由时的方向：把一个 OpenAI-compatible 网关作为 Provider 表的一项接入，HelperMe 其余部分不变。
