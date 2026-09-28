from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import time

from helperme.config import load_app_config
from helperme.llm.adapter import LiteLLMAdapter


async def _stream_once(
    client: LiteLLMAdapter,
    model: str,
    prompt: str,
    thinking: bool,
) -> dict[str, object]:
    started = time.perf_counter()
    stream = await client._router.acompletion(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=64,
        temperature=0,
        stream=True,
        stream_options={"include_usage": True},
    )
    opened = time.perf_counter()
    first_token_at: float | None = None
    output_chars = 0
    usage = None

    async for chunk in stream:
        if chunk.usage is not None:
            usage = chunk.usage
        for choice in chunk.choices:
            delta = choice.delta
            content = getattr(delta, "content", None) or ""
            reasoning = getattr(delta, "reasoning_content", None) or ""
            if first_token_at is None and (content or reasoning):
                first_token_at = time.perf_counter()
            output_chars += len(content) + len(reasoning)

    finished = time.perf_counter()
    return {
        "response_headers_seconds": round(opened - started, 3),
        "first_token_seconds": (
            None if first_token_at is None else round(first_token_at - started, 3)
        ),
        "complete_seconds": round(finished - started, 3),
        "input_tokens": None if usage is None else usage.prompt_tokens,
        "output_tokens": None if usage is None else usage.completion_tokens,
        "output_chars": output_chars,
    }


async def _run_mode(app, prompt: str, thinking: bool, repeats: int):
    router = deepcopy(app.model.router)
    for deployment in router["model_list"]:
        deployment["litellm_params"]["reasoning_effort"] = (
            "high" if thinking else "none"
        )
    model_config = replace(app.model, router=router)
    client_started = time.perf_counter()
    async with LiteLLMAdapter(model_config) as client:
        client_created = time.perf_counter()
        rows = []
        for index in range(repeats):
            row = await _stream_once(client, model_config.active, prompt, thinking)
            row["request"] = index + 1
            rows.append(row)
    return {
        "thinking": thinking,
        "client_create_seconds": round(client_created - client_started, 3),
        "requests": rows,
    }


async def run(args: argparse.Namespace) -> dict[str, object]:
    config_started = time.perf_counter()
    app = load_app_config()
    config_seconds = time.perf_counter() - config_started
    modes = (
        [any(
            deployment["litellm_params"].get("reasoning_effort") not in (None, "none")
            for deployment in app.model.router["model_list"]
        )]
        if args.thinking == "configured"
        else [args.thinking == "on"]
        if args.thinking in {"on", "off"}
        else [True, False]
    )
    results = []
    for thinking in modes:
        result = await _run_mode(app, args.prompt, thinking, args.repeats)
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return {
        "model": app.model.active,
        "config_load_seconds": round(config_seconds, 3),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="测量 OpenAI 兼容模型的响应头、首 token 和完整响应延迟。"
    )
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument(
        "--thinking",
        choices=("configured", "on", "off", "both"),
        default="both",
    )
    parser.add_argument("--prompt", default="只回复数字 2。")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats 必须大于 0")
    result = asyncio.run(run(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
