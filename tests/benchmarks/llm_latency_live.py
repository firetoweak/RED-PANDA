from __future__ import annotations

import argparse
import asyncio
import json
import time

from helperme.config import load_app_config
from thinllm import ChatCompletionsClient
from helperme.llm.config import load_endpoint


async def _stream_once(
    client: ChatCompletionsClient,
    model: str,
    prompt: str,
    thinking: bool,
) -> dict[str, object]:
    started = time.perf_counter()
    first_token_at: float | None = None
    output_chars = 0

    def on_delta(text: str) -> None:
        nonlocal first_token_at, output_chars
        if first_token_at is None:
            first_token_at = time.perf_counter()
        output_chars += len(text)

    result = await client.chat(
        [{"role": "user", "content": prompt}],
        model,
        on_content_delta=on_delta,
        on_reasoning_delta=on_delta if thinking else None,
    )

    finished = time.perf_counter()
    return {
        "first_token_seconds": (
            None if first_token_at is None else round(first_token_at - started, 3)
        ),
        "complete_seconds": round(finished - started, 3),
        "input_tokens": result.usage.input_tokens,
        "output_tokens": result.usage.output_tokens,
        "output_chars": output_chars,
    }


async def _run_mode(app, prompt: str, thinking: bool, repeats: int):
    request_options = {
        "reasoning_effort": "high" if thinking else "none",
        "max_tokens": 64,
        "temperature": 0,
    }
    client_started = time.perf_counter()
    async with ChatCompletionsClient(
        load_endpoint(app.default), request_options=request_options
    ) as client:
        client_created = time.perf_counter()
        rows = []
        for index in range(repeats):
            row = await _stream_once(client, app.default_model, prompt, thinking)
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
        [args.thinking == "on"]
        if args.thinking in {"on", "off"}
        else [True, False]
    )
    results = []
    for thinking in modes:
        result = await _run_mode(app, args.prompt, thinking, args.repeats)
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return {
        "model": app.default_model,
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
        choices=("on", "off", "both"),
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
