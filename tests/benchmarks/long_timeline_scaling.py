"""Measure how roster, origin counting and replay scale with long Agent timelines.

Not a test. Builds a real SQLite session store with several Agents whose
timelines hold many colleague messages, plus non-Agent Journals standing in
for Compact readers and SubAgent children, then times the paths production
takes:

- roster listing (every send, group call and inbox read goes through it):
  the first call builds the index from the Journals, later calls read it,
- origin admission (checked before each Agent message),
- one Session's snapshot and replay (each decision does this).

Usage:
    python -m tests.benchmarks.long_timeline_scaling --agents 4 --events 2000 --extra 40
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from redpanda.assistant.agents import AgentCreated, PrivateMessage, Speaker, fact_arguments
from types import SimpleNamespace

from redpanda.assistant.host.agent_messages import OriginUsage
from redpanda.assistant.host.agent_roster import AgentRoster
from redpanda.assistant.host.session_store import SessionStore
from redpanda.runtime import AgentRuntime, SqliteJournal, replay


async def build(root: Path, agents: int, events: int, extra: int) -> SessionStore:
    store = SessionStore(root / "sessions")
    names = [f"agent-{index}" for index in range(agents)]
    for name in names:
        worktree = root / name
        worktree.mkdir()
        await store.create(name, workspace_id="project", initial_fact=fact_arguments(
            AgentCreated(name=name, workspace_id="project", worktree=str(worktree), branch=name),
            delivery_id="created"))
        runtime = AgentRuntime(SqliteJournal(store.require(name)), None, {})
        for index in range(events):
            sender = names[(names.index(name) + 1) % len(names)]
            message = PrivateMessage(
                message_id=f"{name}-{index}", sender=Speaker(kind="agent", name=sender),
                recipient=name, thread_id=f"thread-{index % 50}", hop=0, reply_to=None,
                text="接口改好了，请核对。" * 4, origin=f"origin-{index % 200}")
            await runtime.receive_domain_fact(name, **fact_arguments(message, delivery_id=message.message_id))
    for index in range(extra):
        await store.create(f"reader-{index}", workspace_id="project")
    return store


async def timed(label, runs, action):
    started = time.perf_counter()
    for _ in range(runs):
        await action()
    print(f"{label}: {(time.perf_counter() - started) / runs * 1000:.1f} ms")


async def main(agents: int, events: int, extra: int, runs: int) -> None:
    with TemporaryDirectory() as directory:
        started = time.perf_counter()
        store = await build(Path(directory), agents, events, extra)
        print(f"built {agents} agents x {events} events + {extra} other journals "
              f"in {time.perf_counter() - started:.1f} s")
        roster = AgentRoster(store)

        async def no_groups(*_arguments):
            return []

        origins = OriginUsage(SimpleNamespace(
            agents=roster, store=store, agent_groups=SimpleNamespace(list=no_groups, messages=no_groups)))

        async def decision_replay():
            events_ = await SqliteJournal(store.require("agent-0")).snapshot("agent-0")
            replay("agent-0", events_)

        await timed("roster index build (once per Host start)", 1, lambda: roster.list("project"))
        await timed("roster list", runs, lambda: roster.list("project"))
        await timed("origin index build (once per Host start)", 1, lambda: origins.count("project", "origin-7"))
        await timed("origin admission (one Agent message)", runs, lambda: origins.count("project", "origin-7"))
        await timed("one timeline snapshot + replay (one decision)", runs, decision_replay)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--agents", type=int, default=4)
    parser.add_argument("--events", type=int, default=2000)
    parser.add_argument("--extra", type=int, default=40)
    parser.add_argument("--runs", type=int, default=3)
    arguments = parser.parse_args()
    asyncio.run(main(arguments.agents, arguments.events, arguments.extra, arguments.runs))
