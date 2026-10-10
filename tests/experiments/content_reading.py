"""Run identical evidence tasks against a pinned baseline and the working tree.

python -m tests.experiments.content_reading --output tests/.live_workspace/content-reading
Uses the configured model; writes requests, results and usage without credentials.
"""
import argparse
import asyncio
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import types

from thinllm import ChatCompletionsClient
from redpanda.config import load_app_config
from redpanda.llm.config import load_endpoint
from redpanda.assistant.artifacts import MemoryArtifactGateway
from redpanda.assistant import content
from redpanda.assistant.context import projection
from redpanda.assistant.compact import core
from redpanda.runtime import AgentRuntime, InvokeTool, MemoryJournal, ModelDecision, StateProjector, ToolBinding
from redpanda.runtime.dispatcher import AttemptContext
from tests.session_scheduler import settle_session


BASELINE_REVISION = "713aeb8d81aeb77c866b7cf72ad0cc9f94ea756b"


def baseline_module(path, name):
    source = subprocess.run(["git", "show", f"{BASELINE_REVISION}:{path}"], check=True, capture_output=True, encoding="utf-8").stdout
    module = types.ModuleType(name)
    sys.modules[name] = module
    exec(compile(source, path, "exec"), module.__dict__)
    return module


def document(case):
    filler = "\n".join(f"services/service-{i:04d}.yaml:8: connection_timeout_ms: {5000 + i}" for i in range(320 if case == "medium" else 1000))
    if case == "browse":
        heading = "支付服务运行说明\n目录：启动方式、资源管理、服务等待规则、错误处理。\n"
        target = "\n服务等待规则\n来源 docs/payment.md:217\n支付服务的一次请求最长等待 8423 毫秒；超过后返回超时错误。\n"
        value = "8423"
    else:
        heading = "超时配置检索结果。以下是文件路径、行号和匹配原文。\n"
        target = "\nconfig/payment.yaml:217: request_timeout_ms: 13729\nconfig/payment.yaml:218: # 此配置用于支付服务的一次 HTTP 请求。\n"
        value = "13729"
    middle = filler.rfind("\n", 0, len(filler) * 3 // 5)
    return heading + filler[:middle] + target + filler[middle:], value


async def setup(case, variant, old):
    p, c, a = (old["projection"], old["core"], old["artifacts"]) if variant == "baseline" else (projection, core, None)
    gateway = MemoryArtifactGateway()
    projector = p.ModelContextProjector(gateway=gateway)
    text, value = document(case)
    payload = {"ok": True, "code": "SEARCH_RESULT", "data": {"content": text}, "error": None, "hint": "核对配置来源后回答。"}
    decisions = [ModelDecision(command_requests=(InvokeTool("inspect", (("query", "request_timeout"),)),)), ModelDecision(content="资料已取得，等待继续核对。")]
    class Decision:
        async def decide(self, frame):
            return decisions.pop(0)
    async def inspect(context, arguments):
        return p.externalize_tool_result(payload, "experiment", gateway, projector.settings)
    runtime = AgentRuntime(MemoryJournal(), Decision(), {"inspect": ToolBinding(inspect)})
    goal = ("请根据刚才取得的文档，确认支付服务一次请求最多等待多少毫秒，并给出来源。" if case == "browse" else
            "请从刚才的检索结果确认 request_timeout_ms 的值和来源文件、行号。")
    goal += '最终只返回 JSON：{"value_ms":整数或null,"source":"原文中的文件路径及行号"}；证据不足时 value_ms 为 null。'
    if case == "compact":
        await runtime.receive_user_message("experiment", "先检索 request_timeout 的配置资料，稍后核对。", delivery_id="first")
    else:
        await runtime.receive_user_message("experiment", goal, delivery_id="first")
    await settle_session(runtime, "experiment")
    events = await runtime.snapshot("experiment")
    context = c.CompactContext("experiment", events, projector, None)
    context.runtime = runtime
    if case == "compact":
        prepared = projector.prepare(events, StateProjector().project_visible("experiment", events), "experiment", "你是助手。")
        bundle = c.save_document(gateway, "experiment", c.frozen_bundle(projector, events, "experiment", context, prepared))
        material = c.save_document(gateway, "experiment", {"messages": [{"role": "user", "content": c.HANDOFF_PREFIX + "此前已检索 request_timeout 配置资料，尚未核对实际值和来源。"}]})
        await c.CompactBoundary(runtime, None, context, None, None, None).publish({
            "handoff": {"artifact": material, "request": material},
            "window": {"id": "experiment-window", "parent": None, "upto": events[-1].sequence,
                       "cutover": events[-1].sequence, "context": material, "bundle": bundle},
        })
        await runtime.receive_user_message("experiment", goal, delivery_id="next")
        events = await runtime.snapshot("experiment")
    visible = context.visible(events, StateProjector().project_visible("experiment", events))
    prepared = projector.prepare(events, visible, "experiment", "根据工具返回的原文完成用户要求。证据不足时继续读取，不能猜测数值。", prefix=context.prefix)
    if variant == "baseline":
        tools = [a.READ_ARTIFACT_SCHEMA, c.READ_SCHEMA]
        bindings = {**a.read_artifact_binding(gateway), **context.bindings()}
    else:
        tools = [content.READ_CONTENT_SCHEMA, c.FIND_HISTORY_SCHEMA]
        bindings = context.bindings()
    return prepared.messages, tools, bindings, value


async def run_case(client, model, case, variant, old, output):
    messages, schemas, bindings, expected = await setup(case, variant, old)
    trace = {"case": case, "variant": variant, "model": model, "initial_messages": messages.copy(),
             "tools": schemas, "turns": [], "calls": 0, "returned_chars": 0,
             "input_tokens": 0, "output_tokens": 0, "correct": False,
             "evidence_read": expected in json.dumps(messages, ensure_ascii=False)}
    started = time.monotonic()
    try:
        async with asyncio.timeout(300):
            for index in range(24):
                result = await client.chat(messages, model, tools=schemas)
                response = result.response
                trace["input_tokens"] += result.usage.input_tokens
                trace["output_tokens"] += result.usage.output_tokens
                assistant = {**response.message_extensions, "role": "assistant", "content": response.content or None}
                if response.calls:
                    assistant["tool_calls"] = [{"id": call.id, "type": "function", "function": {"name": call.name, "arguments": call.arguments}} for call in response.calls]
                messages.append(assistant)
                turn = {"assistant": assistant, "results": []}
                trace["turns"].append(turn)
                if not response.calls:
                    trace["answer"] = response.content
                    try:
                        answer = json.loads(response.content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())
                    except json.JSONDecodeError:
                        answer = {}
                    source = "docs/payment.md" if case == "browse" else "config/payment.yaml"
                    trace["correct"] = (answer.get("value_ms") == int(expected) and source in answer.get("source", "")
                                        and re.search(r"(?<!\d)217(?!\d)", answer.get("source", "")) is not None
                                        and trace["evidence_read"])
                    break
                for call in response.calls:
                    arguments = json.loads(call.arguments)
                    value = await bindings[call.name].handler(AttemptContext("experiment", f"c{index}", f"a{index}", 1), arguments)
                    encoded = json.dumps(value, ensure_ascii=False)
                    trace["calls"] += 1
                    trace["returned_chars"] += len(encoded)
                    trace["evidence_read"] |= expected in encoded
                    turn["results"].append(value)
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": encoded})
                    print(f"{variant}/{case}: {call.name} call {trace['calls']}, {len(encoded)} chars", flush=True)
    finally:
        trace["seconds"] = round(time.monotonic() - started, 2)
        (output / f"{variant}-{case}.json").write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {k: trace[k] for k in ("case", "variant", "model", "calls", "returned_chars", "input_tokens", "output_tokens", "correct", "evidence_read", "seconds")}
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


async def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--variants", nargs="+", default=["baseline", "current"], choices=["baseline", "current"])
    parser.add_argument("--cases", nargs="+", default=["medium", "known", "browse", "compact"], choices=["medium", "known", "browse", "compact"])
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    old = {name: baseline_module(f"redpanda/assistant/{path}.py", f"content_baseline_{name}") for name, path in
           (("artifacts", "artifacts"), ("projection", "context/projection"), ("core", "compact/core"))} if "baseline" in args.variants else {}
    config = load_app_config()
    summaries = []
    async with ChatCompletionsClient(load_endpoint(config.default)) as client:
        for case in args.cases:
            for variant in args.variants:
                summaries.append(await run_case(client, config.default_model, case, variant, old, args.output))
                (args.output / "summary.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
