"""Session-owned capability catalog projection."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Mapping, Sequence

from redpanda.assistant.toolsets import ToolsetDescriptor
from redpanda.runtime import DomainFactCommitted, Event
from redpanda.runtime.json_values import thaw_value


CATALOG = "assistant.catalog"
CATALOG_SOURCE = "assistant.catalog"


@dataclass(frozen=True, slots=True)
class CatalogSkill:
    id: str
    description: str
    revision: int


@dataclass(frozen=True, slots=True)
class CatalogCli:
    id: str
    description: str
    version: str | None
    revision: int


@dataclass(frozen=True, slots=True)
class CapabilityCatalogSnapshot:
    revision: str
    toolsets: tuple[ToolsetDescriptor, ...]
    skills: tuple[CatalogSkill, ...]
    clis: tuple[CatalogCli, ...]
    management: str


class CapabilityCatalog:
    """Commit Registry observations, then apply only the committed Session fact."""

    def __init__(self, surface, skills, clis, management, *, restricted: bool = False) -> None:
        self._surface = surface
        self._skills = skills
        self._clis = clis
        self._management = management
        self._restricted = restricted

    async def sync(self, runtime, session_id: str) -> CapabilityCatalogSnapshot:
        events = await runtime.snapshot(session_id)
        previous = project_catalog(events)
        payload = self._registry_payload(session_id)
        current = _snapshot_from_payload(payload)
        if previous is None or previous.revision != current.revision:
            previous_event_id = _latest_catalog_event_id(events)
            event = await runtime.receive_domain_fact(
                session_id,
                CATALOG,
                payload,
                source=CATALOG_SOURCE,
                delivery_id=f"{previous_event_id or 'initial'}:{current.revision}",
                requests_decision=False,
            )
            current = _snapshot_from_fact(event.payload)
        self._apply(session_id, current)
        return current

    def rehydrate(
        self,
        session_id: str,
        events: Sequence[Event],
    ) -> CapabilityCatalogSnapshot | None:
        snapshot = project_catalog(events)
        if snapshot is not None:
            self._apply(session_id, snapshot)
        return snapshot

    def _registry_payload(self, session_id: str) -> dict[str, object]:
        content: dict[str, object] = {
            "toolsets": sorted(
                (asdict(item) for item in self._surface.registry_descriptors()),
                key=lambda item: item["id"],
            ),
            "skills": self._skills.registry_catalog(),
            "clis": [] if self._restricted else self._clis.registry_catalog(),
            "management": "本会话不提供管理操作。" if self._restricted else self._management.catalog_instruction(session_id),
        }
        return {"revision": _catalog_revision(content), **content}

    def _apply(
        self,
        session_id: str,
        snapshot: CapabilityCatalogSnapshot,
    ) -> None:
        self._surface.apply_catalog(session_id, snapshot.toolsets)
        self._skills.apply_catalog(session_id, snapshot.skills)
        self._clis.apply_catalog(session_id, snapshot.clis)


def project_catalog(
    events: Sequence[Event],
) -> CapabilityCatalogSnapshot | None:
    latest = None
    for event in events:
        payload = event.payload
        if not isinstance(payload, DomainFactCommitted) or payload.fact_type != CATALOG:
            continue
        latest = _snapshot_from_fact(payload)
    return latest


def _snapshot_from_fact(fact: DomainFactCommitted) -> CapabilityCatalogSnapshot:
    if fact.requests_decision:
        raise ValueError("capability catalog fact must not request a decision")
    return _snapshot_from_payload(thaw_value(fact.data))


def _snapshot_from_payload(payload: object) -> CapabilityCatalogSnapshot:
    if not isinstance(payload, Mapping) or set(payload) != {
        "revision",
        "toolsets",
        "skills",
        "clis",
        "management",
    }:
        raise ValueError("capability catalog fields do not match")
    revision = payload["revision"]
    management = payload["management"]
    if type(revision) is not str or not revision.startswith("sha256:"):
        raise ValueError("capability catalog revision is invalid")
    if type(management) is not str or not management:
        raise ValueError("capability catalog management description is invalid")
    toolsets = _toolsets(payload["toolsets"])
    skills = _skills(payload["skills"])
    clis = _clis(payload["clis"])
    content = {
        "toolsets": [asdict(item) for item in toolsets],
        "skills": [asdict(item) for item in skills],
        "clis": [asdict(item) for item in clis],
        "management": management,
    }
    if revision != _catalog_revision(content):
        raise ValueError("capability catalog revision does not match its content")
    return CapabilityCatalogSnapshot(
        revision,
        toolsets,
        skills,
        clis,
        management,
    )


def _toolsets(value: object) -> tuple[ToolsetDescriptor, ...]:
    items = _mapping_items(value, {"id", "description", "revision"}, "toolset")
    result = tuple(
        ToolsetDescriptor(item["id"], item["description"], item["revision"])
        for item in items
    )
    _require_sorted_unique(tuple(item.id for item in result), "toolset")
    return result


def _skills(value: object) -> tuple[CatalogSkill, ...]:
    items = _mapping_items(value, {"id", "description", "revision"}, "skill")
    result: list[CatalogSkill] = []
    for item in items:
        _require_description_and_revision(item, "skill")
        result.append(CatalogSkill(item["id"], item["description"], item["revision"]))
    _require_sorted_unique(tuple(item.id for item in result), "skill")
    return tuple(result)


def _clis(value: object) -> tuple[CatalogCli, ...]:
    items = _mapping_items(
        value,
        {"id", "description", "version", "revision"},
        "cli",
    )
    result: list[CatalogCli] = []
    for item in items:
        _require_description_and_revision(item, "cli")
        version = item["version"]
        if version is not None and (type(version) is not str or not version):
            raise ValueError("catalog cli version is invalid")
        result.append(
            CatalogCli(
                item["id"],
                item["description"],
                version,
                item["revision"],
            )
        )
    _require_sorted_unique(tuple(item.id for item in result), "cli")
    return tuple(result)


def _mapping_items(
    value: object,
    keys: set[str],
    kind: str,
) -> tuple[Mapping[str, object], ...]:
    if type(value) is not list:
        raise ValueError(f"capability catalog {kind}s must be a list")
    items: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != keys:
            raise ValueError(f"catalog {kind} fields do not match")
        items.append(item)
    return tuple(items)


def _require_description_and_revision(
    item: Mapping[str, object],
    kind: str,
) -> None:
    if type(item["id"]) is not str or not item["id"]:
        raise ValueError(f"catalog {kind} id is invalid")
    if type(item["description"]) is not str or not item["description"]:
        raise ValueError(f"catalog {kind} description is invalid")
    if type(item["revision"]) is not int or item["revision"] < 1:
        raise ValueError(f"catalog {kind} revision is invalid")


def _require_sorted_unique(ids: tuple[str, ...], kind: str) -> None:
    if ids != tuple(sorted(ids)) or len(ids) != len(set(ids)):
        raise ValueError(f"catalog {kind} ids must be sorted and unique")


def _catalog_revision(content: Mapping[str, object]) -> str:
    encoded = json.dumps(
        content,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _latest_catalog_event_id(events: Sequence[Event]) -> str | None:
    return next(
        (
            event.event_id
            for event in reversed(events)
            if isinstance(event.payload, DomainFactCommitted)
            and event.payload.fact_type == CATALOG
        ),
        None,
    )
