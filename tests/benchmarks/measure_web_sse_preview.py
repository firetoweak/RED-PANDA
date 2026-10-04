"""Measure whether Web preview deltas stay separate on the SSE socket.

Injects TUI-sized deltas into WebEventHub and records how they arrive on a
real uvicorn TCP connection. This isolates transport coalescing from React.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

import uvicorn

from redpanda.channels.web.app import create_web_app
from redpanda.channels.web.channel import WebChannel
from redpanda.channels.web.hub import WebEventHub


class _Sessions:
    async def release(self, owner) -> None:
        del owner


class _Queries:
    async def list_sessions(self):
        return ()


def _parse_sse_block(block: str) -> tuple[str | None, object | None]:
    event = None
    data = None
    for line in block.splitlines():
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            raw = line.split(":", 1)[1].strip()
            data = json.loads(raw) if raw else None
    return event, data


def _cluster_gaps(gaps_ms: list[float], threshold_ms: float) -> list[int]:
    if not gaps_ms:
        return [1]
    sizes = []
    size = 1
    for gap in gaps_ms:
        if gap <= threshold_ms:
            size += 1
        else:
            sizes.append(size)
            size = 1
    sizes.append(size)
    return sizes


async def _read_http_headers(reader: asyncio.StreamReader) -> bytes:
    header = b""
    while b"\r\n\r\n" not in header:
        chunk = await reader.read(1024)
        if not chunk:
            raise RuntimeError("SSE connection closed before headers")
        header += chunk
    headers, rest = header.split(b"\r\n\r\n", 1)
    status = headers.split(b"\r\n", 1)[0]
    if b"200" not in status:
        raise RuntimeError(f"unexpected SSE status: {status!r}")
    return rest


async def _run_interval(
    hub: WebEventHub,
    host: str,
    port: int,
    interval_ms: int,
    count: int,
) -> dict[str, object]:
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(
        (
            f"GET /api/events HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Accept: text/event-stream\r\n"
            "Connection: keep-alive\r\n"
            "\r\n"
        ).encode()
    )
    await writer.drain()

    leftover = await _read_http_headers(reader)
    buffer = leftover.decode()
    reads: list[tuple[float, int, int]] = []
    arrivals: list[tuple[float, str, int]] = []
    emits: list[tuple[float, str, int]] = []
    connected = asyncio.Event()
    done = asyncio.Event()

    async def consume() -> None:
        nonlocal buffer
        while not done.is_set() or buffer:
            if "\n\n" not in buffer and "\r\n\r\n" not in buffer:
                chunk = await reader.read(4096)
                now = time.perf_counter()
                if not chunk:
                    break
                events_before = len(arrivals)
                buffer += chunk.decode()
                parsed = _drain_events(buffer, arrivals, now)
                buffer = parsed
                reads.append((now, len(chunk), len(arrivals) - events_before))
                if any(name == "connected" for _, name, _ in arrivals):
                    connected.set()
                continue
            now = time.perf_counter()
            buffer = _drain_events(buffer, arrivals, now)
            if any(name == "connected" for _, name, _ in arrivals):
                connected.set()
        connected.set()

    def _drain_events(
        text: str,
        sink: list[tuple[float, str, int]],
        now: float,
    ) -> str:
        while True:
            sep = "\r\n\r\n" if "\r\n\r\n" in text and (
                "\n\n" not in text or text.find("\r\n\r\n") <= text.find("\n\n")
            ) else "\n\n"
            if sep not in text:
                return text
            block, text = text.split(sep, 1)
            event, data = _parse_sse_block(block)
            if event is None:
                continue
            size = 0
            if event == "preview.delta" and isinstance(data, dict):
                size = len(data.get("text") or "")
            sink.append((now, event, size))
        return text

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(connected.wait(), timeout=5)
    await asyncio.sleep(0.05)

    await hub.preview("measure", "started", "output-1", None)
    for index in range(count):
        text = "字"
        emits.append((time.perf_counter(), "preview.delta", len(text)))
        await hub.preview("measure", "delta", "output-1", text)
        if interval_ms:
            await asyncio.sleep(interval_ms / 1000)
    await asyncio.sleep(0.2)
    done.set()
    writer.close()
    await writer.wait_closed()
    await asyncio.wait_for(consumer, timeout=2)

    deltas_in = [item for item in emits if item[1] == "preview.delta"]
    deltas_out = [item for item in arrivals if item[1] == "preview.delta"]
    if len(deltas_out) != len(deltas_in):
        raise RuntimeError(
            f"lost deltas at {interval_ms}ms: sent {len(deltas_in)} got {len(deltas_out)}"
        )

    emit_gaps = [
        (later[0] - earlier[0]) * 1000
        for earlier, later in zip(deltas_in, deltas_in[1:])
    ]
    recv_gaps = [
        (later[0] - earlier[0]) * 1000
        for earlier, later in zip(deltas_out, deltas_out[1:])
    ]
    lags = [
        (arrived[0] - emitted[0]) * 1000
        for emitted, arrived in zip(deltas_in, deltas_out)
    ]
    clusters = _cluster_gaps(recv_gaps, threshold_ms=3)
    events_per_read = [count for _, _, count in reads if count]
    return {
        "interval_ms": interval_ms,
        "count": count,
        "emit_gap_avg_ms": _avg(emit_gaps),
        "recv_gap_avg_ms": _avg(recv_gaps),
        "recv_gap_p50_ms": _quantile(recv_gaps, 0.5),
        "recv_gap_p95_ms": _quantile(recv_gaps, 0.95),
        "lag_avg_ms": _avg(lags),
        "lag_p95_ms": _quantile(lags, 0.95),
        "same_tick_pairs": sum(1 for gap in recv_gaps if gap <= 0.05),
        "cluster_threshold_ms": 3,
        "cluster_sizes": clusters,
        "cluster_max": max(clusters),
        "cluster_count": len(clusters),
        "reads_with_events": len(events_per_read),
        "events_per_read": events_per_read,
        "events_per_read_max": max(events_per_read, default=0),
        "react_jumps_if_batched": len(clusters),
        "chars_per_react_jump": [
            size * len("字") for size in clusters
        ],
    }


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


async def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--count", type=int, default=40)
    parser.add_argument(
        "--intervals",
        default="0,5,20,50",
        help="comma-separated emit intervals in ms",
    )
    args = parser.parse_args(argv)
    intervals = [int(item) for item in args.intervals.split(",") if item.strip()]

    hub = WebEventHub()
    app = create_web_app(WebChannel(_Sessions(), _Queries()), hub)
    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    serve = asyncio.create_task(server.serve())
    try:
        for _ in range(50):
            if server.started:
                break
            await asyncio.sleep(0.05)
        if not server.started:
            raise RuntimeError("uvicorn did not start")

        results = []
        for interval in intervals:
            results.append(
                await _run_interval(
                    hub,
                    args.host,
                    args.port,
                    interval,
                    args.count,
                )
            )
        print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
        print()
        print("interval  emit_gap  recv_gap  lag_p95  same_tick  max_read  clusters  max_cluster")
        for row in results:
            print(
                f"{row['interval_ms']:>8}  "
                f"{row['emit_gap_avg_ms']:8.1f}  "
                f"{row['recv_gap_avg_ms']:8.1f}  "
                f"{row['lag_p95_ms']:7.1f}  "
                f"{row['same_tick_pairs']:9}  "
                f"{row['events_per_read_max']:8}  "
                f"{row['cluster_count']:8}  "
                f"{row['cluster_max']:11}"
            )
    finally:
        server.should_exit = True
        await serve


if __name__ == "__main__":
    asyncio.run(main())
