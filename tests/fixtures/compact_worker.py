from __future__ import annotations

import asyncio
from inspect import isawaitable
import json
from pathlib import Path

from helperme.config import AssistantConfig
from helperme.llm.types import LLMCallResult, LLMResponse, LLMUsage, ToolCall


HANDOFF = """## 用户要什么
用户要求讨论并保留 TAIL_KEEP。
## 目前做到哪里
旧模型已回应早期输入；完成声明未经独立验证。
## 还有什么未解决
等待用户下一条输入。
## 接手所需的证据与入口
按来源回读旧会话。
"""


class CompactLlm:
    def __init__(self, workspace):
        self.workspace = workspace

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def chat(self, messages, model, *, tools=None, on_content_delta=None, on_reasoning_delta=None):
        if any("<self_handoff>" in str(m["content"]) for m in messages):
            request = self.workspace / "handoff_request.json"
            if not request.exists():
                request.write_text(
                    json.dumps(
                        {"messages": messages, "tools": tools},
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            (self.workspace / "compact_started").touch()
            if (self.workspace / "fail_compact").exists():
                from helperme.llm.api import LLMProviderError

                raise LLMProviderError("compactor provider failed")
            if (self.workspace / "invalid_handoff").exists():
                return LLMCallResult(
                    LLMResponse(content="", calls=()),
                    LLMUsage(input_tokens=0, output_tokens=5),
                )
            if (self.workspace / "read_compact").exists() or (self.workspace / "repeat_reads").exists():
                tool_results = [m for m in messages if m["role"] == "tool" and "source" in str(m["content"])]
                reads = 10 if (self.workspace / "repeat_reads").exists() else 1
                if len(tool_results) < reads:
                    return LLMCallResult(LLMResponse(content="回读", calls=(ToolCall(
                        "read-source", "read_compact_source", json.dumps({
                            "source": "chat", "kind": "view", "reference": "", "offset": 0, "limit": 1000
                        })),)), LLMUsage(input_tokens=0, output_tokens=5))
            if (self.workspace / "write_compact").exists():
                return LLMCallResult(LLMResponse(content="", calls=(ToolCall(
                    "write", "write_file", '{"path":"forbidden.txt","content":"bad"}'
                ),)), LLMUsage(input_tokens=0, output_tokens=5))
            while not (self.workspace / "release_compact").exists():
                await asyncio.sleep(0.02)
            response = LLMResponse(content=HANDOFF, calls=())
        else:
            # Record actual requests to prove S1 sees the tail and no old execution replays.
            with (self.workspace / "requests.jsonl").open(
                "a", encoding="utf-8"
            ) as file:
                file.write(json.dumps(messages, ensure_ascii=False) + "\n")
            last_user = next(
                (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
            )
            if (
                last_user == "RUN_TOOL"
                and not (self.workspace / "tool_started").exists()
            ):
                response = LLMResponse(
                    content="",
                    calls=(ToolCall("read", "read_file", '{"path":"input.txt"}'),),
                )
            else:
                response = LLMResponse(content="正常回复", calls=())
        if response.content and on_content_delta is not None:
            emitted = on_content_delta(response.content)
            if isawaitable(emitted):
                await emitted
        return LLMCallResult(response, LLMUsage(input_tokens=0, output_tokens=5))


class ChildCompactLlm(CompactLlm):
    async def chat(self, messages, model, *, tools=None, on_content_delta=None, on_reasoning_delta=None):
        if any("<self_handoff>" in str(m["content"]) for m in messages):
            return await super().chat(
                messages, model, tools=tools,
                on_content_delta=on_content_delta,
                on_reasoning_delta=on_reasoning_delta,
            )
        if "你是一个被委派的子 Agent" in messages[0]["content"]:
            while (self.workspace / "hold_child").exists() and not (self.workspace / "release_child").exists():
                await asyncio.sleep(0.02)
            if not any(message["role"] == "tool" for message in messages):
                return LLMCallResult(
                    LLMResponse(content="", calls=(ToolCall(
                        "read", "read_file", '{"path":"input.txt"}'
                    ),)),
                    LLMUsage(input_tokens=0, output_tokens=5),
                )
            return LLMCallResult(
                LLMResponse(content="", calls=(ToolCall(
                    "report", "report", '{"summary":"已完成子任务"}'
                ),)),
                LLMUsage(input_tokens=0, output_tokens=5),
            )
        return await super().chat(
            messages, model, tools=tools,
            on_content_delta=on_content_delta,
            on_reasoning_delta=on_reasoning_delta,
        )


def config_for(workspace: Path):
    return AssistantConfig(
        model_name="compact-test",
        model_context_limit=60000,
        input_budget_ratio=0.9,
        llm=CompactLlm(workspace),
        compact_threshold_ratio=0.55,
    )


def child_config_for(workspace: Path):
    from dataclasses import replace

    return replace(
        config_for(workspace),
        llm=ChildCompactLlm(workspace),
        model_context_limit=8000,
    )


def tool_config(workspace: Path):
    from helperme.assistant.host import worker
    from helperme.runtime import ToolBinding

    build = worker.build_assistant_assembly

    async def blocked_read(context, arguments):
        (workspace / "tool_started").touch()
        while not (workspace / "release_tool").exists():
            await asyncio.sleep(0.02)
        with (workspace / "tool_count").open("a") as file:
            file.write("executed\n")
        return {"ok": True, "code": "FILE_READ", "data": {"text": "TOOL_EVIDENCE" + ("X" * 20000)}}

    async def assembly(*args, **kwargs):
        result = await build(*args, **kwargs)
        result.runtime.bind_tool("read_file", ToolBinding(blocked_read))
        return result

    worker.build_assistant_assembly = assembly
    return config_for(workspace)
