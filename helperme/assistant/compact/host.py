"""Background jobs publish a window into the same business Session."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from inspect import isawaitable
from helperme.assistant.artifacts import FileArtifactGateway
from helperme.assistant.compact.core import (
    TASK,
    HANDOFF_PREFIX,
    compact_seed,
    load_document,
    save_document,
    bounded_recent_tail,
    projected_tail,
)
from helperme.assistant.compact.store import CompactStore
from helperme.assistant.context.budget import InputBudget, TiktokenEstimator
from helperme.assistant.context.projection import ModelContextBudgetExceeded
from helperme.assistant.host.ipc import ProcessFailure, WorkerFailed
from helperme.assistant.subagent.subagent import project_parent, project_returned
from helperme.runtime import SqliteJournal


def seed_fact(kind, data, *, continuing):
    return dict(
        fact_type=kind,
        data=data,
        source="compact",
        delivery_id=kind,
        requests_decision=continuing,
    )


class CompactHost:
    def __init__(self, host):
        self.host = host
        self.store = CompactStore(host.store.root)
        self.gateway = FileArtifactGateway(host.store.root)
        self.locks = {}
        self.activating = set()

    def notify_status(self, session):
        if self.host.conversation_status_sink is None:
            return
        emitted = self.host.conversation_status_sink(self.store.status(session))
        if isawaitable(emitted):
            self.host._track(session, asyncio.ensure_future(emitted))

    def lock(self, session):
        return self.locks.setdefault(session, asyncio.Lock())

    async def _source_returned(self, source):
        events = await SqliteJournal(self.host.store.require(source)).snapshot(source)
        return project_parent(events) is not None and project_returned(events)

    def _retire(self, job, *, stop_reader=True):
        self.store.retire(job["reader"])
        self.notify_status(job["source"])
        if stop_reader:
            worker = self.host.workers.get(job["reader"])
            if worker is not None and not worker.exited.is_set():
                self.host.intentionally_stopped.add(job["reader"])
                worker.intentionally_stopped = True
                if worker.process.is_alive():
                    worker.process.terminate()

    async def retire(self, source):
        async with self.lock(source):
            job = self.store.job(source)
            if job is not None:
                self._retire(job)

    def activate(self, session):
        if session in self.activating or self.host.closed:
            return
        self.activating.add(session)

        async def resume():
            try:
                await self.host.request("resume", session, {})
            finally:
                self.activating.remove(session)

        self.host._track(session, asyncio.create_task(resume()))

    async def ensure_reader(self, job):
        reader = job["reader"]
        if job["failure"] is not None:
            return
        if await self._source_returned(job["source"]):
            self._retire(job)
            return
        material = json.loads(job["bundle"])
        data = dict(
            source=job["source"],
            upto=job["upto"],
            window=job["window"],
            **material,
        )
        fact = seed_fact(TASK, data, continuing=True)
        if not self.host.store.path(reader).parent.exists():
            await self.host.store.create(
                reader,
                workspace_id=await self.host.bound_workspace_id(job["source"]),
                initial_fact=fact,
            )
        else:
            events = await SqliteJournal(self.host.store.require(reader)).snapshot(
                reader
            )
            seed = compact_seed(events)
            if seed is None or seed[1] != data:
                raise ValueError("invalid handoff worker seed")
        if reader not in self.host.workers:
            self.activate(reader)

    async def recover_prepared(self, source):
        job = self.store.job(source)
        if job is None or job["prepared"] is None or job["failure"] is not None:
            return False
        if await self._source_returned(source):
            self._retire(job)
            return None
        published = await self.host.request("compact_publish", source, json.loads(job["prepared"]))
        if not published:
            self._retire(job)
            return None
        self.store.publish(job["reader"])
        self.notify_status(source)
        return True

    async def boundary(self, source, arguments):
        async with self.lock(source):
            if await self._source_returned(source):
                job = self.store.job(source)
                if job is not None:
                    self._retire(job)
                return "wait"
            recovery = await self.recover_prepared(source)
            if recovery is True:
                return "continue"
            if recovery is None:
                return "wait"
            job = self.store.job(source)
            if job is None:
                if not arguments["pressure"]:
                    return "continue"
                snapshot = await self.host.request("compact_snapshot", source, {})
                if not snapshot["safe"]:
                    return "wait" if arguments["over_budget"] else "continue"
                job = self.store.start(source, snapshot)
                self.notify_status(source)
            if job["failure"] is not None:
                if arguments["over_budget"]:
                    events = await SqliteJournal(self.host.store.require(source)).snapshot(source)
                    if project_parent(events) is not None:
                        return {"failure": ProcessFailure(**json.loads(job["failure"])).render()}
                return "wait" if arguments["over_budget"] else "continue"
            if job["summary"] is None:
                await self.ensure_reader(job)
                return "wait" if arguments["over_budget"] else "continue"
            snapshot = await self.host.request("compact_snapshot", source, {})
            if not snapshot["safe"]:
                return "wait" if arguments["over_budget"] else "continue"
            try:
                self.prepare_window(job, snapshot)
            except ModelContextBudgetExceeded as error:
                failure = ProcessFailure.capture(error)
                # Publication failure is terminal too; retain the accepted summary for inspection.
                self.store.fail_publication(job["reader"], asdict(failure))
                self.notify_status(source)
                self.host.failures.put_nowait(WorkerFailed(job["reader"], failure))
                if arguments["over_budget"]:
                    events = await SqliteJournal(self.host.store.require(source)).snapshot(source)
                    if project_parent(events) is not None:
                        return {"failure": failure.render()}
                return "wait" if arguments["over_budget"] else "continue"
            recovery = await self.recover_prepared(source)
            return "wait" if recovery is None else "continue"

    def prepare_window(self, job, snapshot):
        if snapshot["window"] != job["window"]:
            raise ValueError("stale handoff")
        source = job["source"]
        material = json.loads(job["bundle"])
        inherited = load_document(self.gateway, source, material["inherited"])
        bundle = load_document(self.gateway, source, snapshot["bundle"])
        p, q = job["upto"], snapshot["position"]
        provenance = {
            "source": source,
            "source_window": job["window"],
            "upto": p,
            "request": material["inherited"],
        }
        before = [
            {
                "role": "user",
                "content": HANDOFF_PREFIX
                + json.dumps(provenance, ensure_ascii=False)
                + "\n"
                + job["summary"],
            }
        ]
        if snapshot["catalog"] is not None:
            before.append(
                {
                    "role": "user",
                    "content": "<capability_catalog>\n"
                    + json.dumps(
                        {"fact": "assistant.catalog", "data": snapshot["catalog"]},
                        ensure_ascii=False,
                    )
                    + "\n</capability_catalog>",
                }
            )
        after = projected_tail(bundle["records"], p, q)
        budget = InputBudget(
            TiktokenEstimator(),
            context_limit=snapshot["context_limit"],
            input_ratio=snapshot["input_ratio"],
        )
        recent = bounded_recent_tail(
            inherited["recent"],
            before=before,
            after=after,
            system_prompt=snapshot["prompt"],
            tools=snapshot["tools"],
            budget=budget,
            tail_budget_tokens=int(
                budget.input_budget_tokens * snapshot["compact_threshold_ratio"]
            ),
        )
        messages = [
            *before,
            *(record["message"] for record in recent),
            *after,
        ]
        context = save_document(self.gateway, source, {"messages": messages})
        handoff = save_document(
            self.gateway, source, {"text": job["summary"], **provenance}
        )
        self.store.prepare(
            job["reader"],
            {
                "handoff": {"reader": job["reader"], "artifact": handoff, **provenance},
                "window": {
                    "id": job["reader"],
                    "parent": job["window"],
                    "upto": p,
                    "cutover": q,
                    "recent_tail_start": recent[0]["sequence"]
                    if recent
                    else p + 1,
                    "context": context,
                    "bundle": snapshot["bundle"],
                },
            },
        )

    async def complete(self, reader, arguments):
        job = self.store.reader_job(reader)
        if job is None:
            raise ValueError("unknown handoff worker")
        async with self.lock(job["source"]):
            if job["published"] == 2:
                return
            if await self._source_returned(job["source"]):
                self._retire(job, stop_reader=False)
                return
            self.store.finish(reader, arguments["handoff"])
            self.notify_status(job["source"])

        async def ready():
            await self.host.request("compact_ready", job["source"], {})

        self.host._track(job["source"], asyncio.create_task(ready()))

    async def application(self, operation, session, arguments):
        async with self.lock(session):
            self.host.store.require(session)
            job = self.store.job(session)
            if job is not None and await self._source_returned(session):
                self._retire(job)
            else:
                await self.recover_prepared(session)
            result = await self.host.request(operation, session, arguments)
            if operation in {"resume", "view"}:
                job = self.store.job(session)
                if (
                    job is not None
                    and job["summary"] is None
                    and job["failure"] is None
                ):
                    await self.ensure_reader(job)
            return result
