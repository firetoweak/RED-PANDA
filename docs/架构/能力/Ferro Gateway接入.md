# Ferro Gateway 接入

HelperMe 只拥有模型调用的窄协议 `LLMApi`。项目提供 Ferro Gateway 的安装与配置，Ferro 作为独立进程运行并通过 HTTP 提供模型服务。Ferro 负责 Provider 凭据、模型路由、重试与 fallback；HelperMe 从 Ferro 提供的模型中选择一个，并按 OpenAI-compatible Chat Completions 协议调用，不在进程内加载模型 SDK 或路由器。

```text
Assistant → LLMApi → Worker LLM Port → Host LLM Client → Ferro Gateway → Provider
```

Host 持有一个 LLM 客户端并跨 Session 复用。Worker 仍通过 Host LLM port 调用，不能直接连接 Ferro。Ferro 的进程与配置独立于 HelperMe Runtime；使用者显式启动和停止 Ferro，HelperMe 不负责监督其生命周期。

LLM 客户端只负责 HelperMe 调用契约与 HTTP/SSE 协议之间的转换：内容增量和推理增量分开投递；工具调用分块重组为完整调用；usage 与模型消息扩展进入现有归一化结果。图片附件在请求边界编码。工具执行、上下文投影、持久化与 Agent 循环始终归 HelperMe 所有。

HelperMe 只保存当前所选的 Ferro 模型标识。Provider 认证、路由、重试和 fallback 不进入 HelperMe 配置或客户端逻辑。Ferro 配置的 MCP 工具注入不用于 HelperMe 调用；HelperMe 在请求中发送自己的工具集并拥有工具执行闭环。

适配器只把明确识别的认证、上下文超限、暂态传输和服务错误转换为 HelperMe 的 LLM 错误类型。未识别的内部异常原样暴露；无效 HTTP/SSE 响应作为无效模型响应处理。HelperMe 不猜测错误文案含义，也不自行重试模型请求。

## 明确不做

- 不由 HelperMe Runtime 自动启动、关闭或监督 Ferro；不提供 Ferro Admin API 或 Provider 客户端。
- 不保留 LiteLLM、进程内 Router、旧 Router 配置或兼容路径。
- 不将 Ferro 或 Provider 的路由结果交给 Worker、Runtime 或 Assistant 基础脚手架解释。
- 不把模型流式预览写入 Journal；只有完整、有效的模型结果参与 Step 提交。
