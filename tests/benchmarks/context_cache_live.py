from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from contextlib import AsyncExitStack
from dataclasses import dataclass
import json
from pathlib import Path
import secrets
import time

import tiktoken
from helperme.paths import runtime_data_root


MANIFEST_SCHEMA = "decision-replay-manifest/v1"


@dataclass(frozen=True, slots=True)
class ReplayRequest:
    journal_position: int
    messages: list[dict[str, object]]
    tools: list[dict[str, object]] | None


def _load_manifests(drawer: Path) -> list[dict[str, object]]:
    manifests: list[dict[str, object]] = []
    for path in (drawer / "artifacts").glob("art_*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if payload.get("schema") == MANIFEST_SCHEMA:
            manifests.append(payload)
    manifests.sort(
        key=lambda item: (
            item["decision_basis"]["observed_journal_position"],
            item["decision_basis"]["decision_cursor"],
        )
    )
    return manifests


def _default_drawer(root: Path) -> Path:
    candidates = [
        (len(_load_manifests(path)), path)
        for path in root.iterdir()
        if path.is_dir()
    ]
    if not candidates:
        raise RuntimeError("没有可用的 Decision Replay Manifest")
    return max(candidates, key=lambda item: item[0])[1]


def _request(manifest: dict[str, object]) -> ReplayRequest:
    basis = manifest["decision_basis"]
    request = manifest["request"]
    return ReplayRequest(
        journal_position=basis["observed_journal_position"],
        messages=deepcopy(request["messages"]),
        tools=deepcopy(request["tools"]),
    )


def _canonical_tool_contents(
    requests: list[ReplayRequest],
) -> dict[str, object]:
    contents: dict[str, object] = {}
    for request in requests:
        for message in request.messages:
            if message.get("role") == "tool":
                contents.setdefault(message["tool_call_id"], message["content"])
    return contents


def _with_tool_contents(
    request: ReplayRequest,
    contents: dict[str, object],
) -> ReplayRequest:
    messages = deepcopy(request.messages)
    for message in messages:
        if message.get("role") == "tool":
            message["content"] = contents[message["tool_call_id"]]
    return ReplayRequest(request.journal_position, messages, deepcopy(request.tools))


def build_variants(
    manifests: list[dict[str, object]],
    *,
    batch_clear_tokens: int,
) -> dict[str, list[ReplayRequest]]:
    current = [_request(item) for item in manifests]
    canonical = _canonical_tool_contents(current)
    no_age = [_with_tool_contents(item, canonical) for item in current]

    encoding = tiktoken.get_encoding("o200k_base")

    def estimate(messages):
        return len(encoding.encode_ordinary(json.dumps(messages, ensure_ascii=False)))
    dehydrated_contents: dict[str, object] = {}
    active: set[str] = set()
    pending: set[str] = set()
    batch: list[ReplayRequest] = []

    def estimated_saving(call_id: str) -> int:
        full = {"role": "tool", "tool_call_id": call_id, "content": canonical[call_id]}
        stub = {
            "role": "tool",
            "tool_call_id": call_id,
            "content": dehydrated_contents[call_id],
        }
        return max(
            0,
            estimate([full]) - estimate([stub]),
        )

    for request in current:
        for message in request.messages:
            if message.get("role") != "tool":
                continue
            call_id = message["tool_call_id"]
            if message["content"] != canonical[call_id]:
                dehydrated_contents.setdefault(call_id, message["content"])
                if call_id not in active:
                    pending.add(call_id)
        if sum(estimated_saving(call_id) for call_id in pending) >= batch_clear_tokens:
            active.update(pending)
            pending.clear()
        selected = {
            call_id: (
                dehydrated_contents[call_id]
                if call_id in active
                else content
            )
            for call_id, content in canonical.items()
        }
        batch.append(_with_tool_contents(request, selected))

    return {"no_age": no_age, "sliding": current, "batch": batch}


async def run(args: argparse.Namespace) -> dict[str, object]:
    from helperme.config import load_app_config
    from helperme.llm.config import load_gateway_config
    from helperme.llm.ferro_client import FerroClient

    root = runtime_data_root()
    drawer = Path(args.drawer).resolve() if args.drawer else _default_drawer(root)
    manifests = _load_manifests(drawer)
    if len(manifests) < 2:
        raise RuntimeError("缓存实验至少需要两个 Replay Manifest")
    variants = build_variants(
        manifests,
        batch_clear_tokens=args.batch_clear_tokens,
    )
    if args.variant is not None:
        variants = {args.variant: variants[args.variant]}
    if args.steps is not None:
        manifests = manifests[-args.steps :]
        variants = {
            name: requests[-args.steps :]
            for name, requests in variants.items()
        }
    app = load_app_config()
    salts = {name: secrets.token_urlsafe(32) for name in variants}
    rows: list[dict[str, object]] = []
    clients = {
        name: FerroClient(
            load_gateway_config(),
            request_options={
                "max_tokens": 1,
                "temperature": 0,
                "cache_salt": salt,
            },
        )
        for name, salt in salts.items()
    }

    async with AsyncExitStack() as clients_scope:
        for client in clients.values():
            await clients_scope.enter_async_context(client)
        for step_index in range(len(manifests)):
            names = list(variants)
            offset = step_index % len(names)
            names = names[offset:] + names[:offset]
            for name in names:
                request = variants[name][step_index]
                started = time.perf_counter()
                completion = await clients[name].chat(
                    request.messages,
                    app.model.active,
                    tools=request.tools,
                )
                elapsed = time.perf_counter() - started
                usage = completion.usage
                cached = usage.cached_input_tokens
                row = {
                    "variant": name,
                    "step": step_index + 1,
                    "journal_position": request.journal_position,
                    "prompt_tokens": usage.input_tokens,
                    "cached_tokens": cached,
                    "uncached_tokens": usage.input_tokens - cached,
                    "cache_rate": cached / usage.input_tokens,
                    "elapsed_seconds": elapsed,
                }
                rows.append(row)
            latest = [row for row in rows if row["step"] == step_index + 1]
            status = " ".join(
                f"{row['variant']}={row['cached_tokens']}/{row['prompt_tokens']}"
                for row in latest
            )
            print(f"step {step_index + 1}/{len(manifests)} {status}", flush=True)

    aggregates: dict[str, dict[str, object]] = {}
    for name in variants:
        selected = [row for row in rows if row["variant"] == name]
        prompt = sum(row["prompt_tokens"] for row in selected)
        cached = sum(row["cached_tokens"] for row in selected)
        uncached = sum(row["uncached_tokens"] for row in selected)
        aggregates[name] = {
            "requests": len(selected),
            "prompt_tokens": prompt,
            "cached_tokens": cached,
            "uncached_tokens": uncached,
            "cache_rate": cached / prompt,
            "elapsed_seconds": sum(row["elapsed_seconds"] for row in selected),
            "max_prompt_tokens": max(row["prompt_tokens"] for row in selected),
        }
    return {
        "drawer": str(drawer),
        "batch_clear_tokens": args.batch_clear_tokens,
        "aggregates": aggregates,
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--drawer")
    parser.add_argument("--steps", type=int)
    parser.add_argument("--batch-clear-tokens", type=int, default=5_000)
    parser.add_argument(
        "--variant",
        choices=("no_age", "sliding", "batch"),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = asyncio.run(run(args))
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(json.dumps(result["aggregates"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
