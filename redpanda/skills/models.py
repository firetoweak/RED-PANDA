from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Literal
from typing import Any, cast


_SKILL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def validate_skill_id(skill_id: str) -> str:
    if not isinstance(skill_id, str) or not _SKILL_ID_PATTERN.fullmatch(skill_id):
        raise ValueError(
            "Skill name 必须匹配 ^[a-z0-9][a-z0-9-]{0,63}$"
        )
    return skill_id


def validate_skill_description(description: str) -> str:
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Skill description 不能为空")
    if description != description.strip():
        raise ValueError("Skill description 不能包含首尾空白")
    if "\n" in description or "\r" in description:
        raise ValueError("Skill description 必须是单行文本")
    if len(description) > 1_000:
        raise ValueError("Skill description 超出 1000 字符限制")
    return description


SkillSourceKind = Literal["local", "github", "url"]


@dataclass(frozen=True)
class SkillSourceRef:
    kind: SkillSourceKind
    locator: str
    requested_ref: str | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not str:
            raise TypeError("Skill source kind 必须是 string")
        if self.kind not in {"local", "github", "url"}:
            raise ValueError(f"不支持的 Skill source kind: {self.kind}")
        if type(self.locator) is not str:
            raise TypeError("Skill source locator 必须是 string")
        if not self.locator.strip():
            raise ValueError("Skill source locator 不能为空")
        if self.requested_ref is not None:
            if type(self.requested_ref) is not str:
                raise TypeError("Skill requested_ref 必须是 string|null")
            if not self.requested_ref:
                raise ValueError("Skill requested_ref 不能为空")

    def to_dict(self) -> dict[str, str | None]:
        return {
            "kind": self.kind,
            "locator": self.locator,
            "requested_ref": self.requested_ref,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "SkillSourceRef":
        _require_exact_keys(
            value,
            {"kind", "locator", "requested_ref"},
            "Skill source",
        )
        requested_ref = value["requested_ref"]
        return cls(
            kind=cast(SkillSourceKind, _require_str(value["kind"], "kind")),
            locator=_require_str(value["locator"], "locator"),
            requested_ref=_require_optional_str(
                requested_ref,
                "requested_ref",
            ),
        )


@dataclass(frozen=True)
class SkillFile:
    relative_path: str
    content: bytes

    def __post_init__(self) -> None:
        if type(self.relative_path) is not str or not self.relative_path:
            raise ValueError("Skill file relative_path 必须是非空 string")
        if type(self.content) is not bytes:
            raise TypeError("Skill file content 必须是 bytes")

    @property
    def size(self) -> int:
        return len(self.content)


@dataclass(frozen=True)
class SkillBundle:
    name: str
    description: str
    main_instructions: str
    source: SkillSourceRef
    resolved_ref: str
    content_hash: str
    files: tuple[SkillFile, ...]

    def __post_init__(self) -> None:
        validate_skill_id(self.name)
        validate_skill_description(self.description)
        if type(self.main_instructions) is not str:
            raise TypeError("Skill main_instructions 必须是 string")
        if type(self.source) is not SkillSourceRef:
            raise TypeError("Skill source 必须是 SkillSourceRef")
        if type(self.resolved_ref) is not str:
            raise TypeError("Skill resolved_ref 必须是 string")
        if not self.resolved_ref.strip():
            raise ValueError("Skill resolved_ref 不能为空")
        if type(self.content_hash) is not str:
            raise TypeError("Skill content_hash 必须是 string")
        if not self.content_hash.strip():
            raise ValueError("Skill content_hash 不能为空")
        if type(self.files) is not tuple or any(
            type(item) is not SkillFile for item in self.files
        ):
            raise TypeError("Skill files 必须是 SkillFile tuple")
        paths = [item.relative_path for item in self.files]
        if "SKILL.md" not in paths:
            raise ValueError("SkillBundle 必须包含 SKILL.md")
        if len(paths) != len(set(paths)):
            raise ValueError("SkillBundle 不能包含重复路径")


@dataclass(frozen=True)
class SkillPackageLimits:
    max_files: int = 512
    max_file_bytes: int = 2 * 1024 * 1024
    max_total_bytes: int = 20 * 1024 * 1024
    max_main_instruction_chars: int = 100_000

    def __post_init__(self) -> None:
        values = (
            self.max_files,
            self.max_file_bytes,
            self.max_total_bytes,
            self.max_main_instruction_chars,
        )
        if any(type(value) is not int for value in values):
            raise TypeError("Skill package limits 必须是 int")
        if min(values) <= 0:
            raise ValueError("Skill package limits 必须大于 0")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("datetime 必须是非空 ISO 8601 字符串")
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _require_aware_datetime(value: object, label: str) -> None:
    if type(value) is not datetime or value.tzinfo is None:
        raise TypeError(f"{label} 必须是带时区的 datetime")


def _require_exact_keys(
    value: dict[str, object],
    expected: set[str],
    label: str,
) -> None:
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{label} 字段不匹配: "
            f"missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _require_str(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} 必须是 string")
    return value


def _require_optional_str(value: object, label: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{label} 必须是 string|null")
    return value


def _require_int(value: object, label: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} 必须是 int")
    return value


def _require_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} 必须是 bool")
    return value


def _require_string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise ValueError(f"{label} 必须是 string array")
    return tuple(value)


@dataclass(frozen=True)
class SkillRecord:
    name: str
    description: str
    source: SkillSourceRef
    resolved_ref: str
    content_hash: str
    enabled: bool = False
    revision: int = 1
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        validate_skill_id(self.name)
        validate_skill_description(self.description)
        if type(self.source) is not SkillSourceRef:
            raise TypeError("Skill record source 必须是 SkillSourceRef")
        if type(self.resolved_ref) is not str:
            raise TypeError("Skill resolved_ref 必须是 string")
        if not self.resolved_ref.strip():
            raise ValueError("Skill resolved_ref 不能为空")
        if type(self.content_hash) is not str:
            raise TypeError("Skill content_hash 必须是 string")
        if not self.content_hash.strip():
            raise ValueError("Skill content_hash 不能为空")
        if type(self.enabled) is not bool:
            raise TypeError("Skill enabled 必须是 bool")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("Skill revision 必须大于 0")
        _require_aware_datetime(self.created_at, "created_at")
        _require_aware_datetime(self.updated_at, "updated_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "source": self.source.to_dict(),
            "resolved_ref": self.resolved_ref,
            "content_hash": self.content_hash,
            "enabled": self.enabled,
            "revision": self.revision,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "SkillRecord":
        _require_exact_keys(
            value,
            {
                "name",
                "description",
                "source",
                "resolved_ref",
                "content_hash",
                "enabled",
                "revision",
                "created_at",
                "updated_at",
            },
            "Skill record",
        )
        raw_source = value["source"]
        if not isinstance(raw_source, dict):
            raise ValueError("Skill record source 必须是 object")
        return cls(
            name=_require_str(value["name"], "name"),
            description=_require_str(value["description"], "description"),
            source=SkillSourceRef.from_dict(raw_source),
            resolved_ref=_require_str(value["resolved_ref"], "resolved_ref"),
            content_hash=_require_str(value["content_hash"], "content_hash"),
            enabled=_require_bool(value["enabled"], "enabled"),
            revision=_require_int(value["revision"], "revision"),
            created_at=_parse_datetime(value["created_at"]),
            updated_at=_parse_datetime(value["updated_at"]),
        )


@dataclass(frozen=True)
class SkillManifestDiff:
    added: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for label, paths in (
            ("added", self.added),
            ("modified", self.modified),
            ("deleted", self.deleted),
        ):
            if type(paths) is not tuple or any(
                type(path) is not str or not path for path in paths
            ):
                raise ValueError(f"Skill manifest {label} 必须是非空 string tuple")
            if len(paths) != len(set(paths)):
                raise ValueError(f"Skill manifest {label} 包含重复路径")
        if (
            set(self.added) & set(self.modified)
            or set(self.added) & set(self.deleted)
            or set(self.modified) & set(self.deleted)
        ):
            raise ValueError("Skill manifest 同一路径不能属于多个变更类别")

    @property
    def changed(self) -> bool:
        return bool(self.added or self.modified or self.deleted)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "added": list(self.added),
            "modified": list(self.modified),
            "deleted": list(self.deleted),
        }
        categories: dict[str, dict[str, list[str]]] = {}
        for change, paths in (
            ("added", self.added),
            ("modified", self.modified),
            ("deleted", self.deleted),
        ):
            for path in paths:
                category = _skill_file_category(path)
                categories.setdefault(category, {
                    "added": [],
                    "modified": [],
                    "deleted": [],
                })[change].append(path)
        payload["skill_md_changed"] = "SKILL.md" in {
            *self.added,
            *self.modified,
            *self.deleted,
        }
        payload["categories"] = categories
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SkillManifestDiff":
        _require_exact_keys(
            value,
            {
                "added",
                "modified",
                "deleted",
                "skill_md_changed",
                "categories",
            },
            "Skill manifest diff",
        )
        result = cls(
            added=_require_string_tuple(value["added"], "added"),
            modified=_require_string_tuple(value["modified"], "modified"),
            deleted=_require_string_tuple(value["deleted"], "deleted"),
        )
        if result.to_dict() != value:
            raise ValueError("Skill manifest diff 派生字段不一致")
        return result


def _skill_file_category(path: str) -> str:
    if path == "SKILL.md":
        return "instructions"
    prefix = path.split("/", 1)[0]
    if prefix in {"references", "scripts", "templates", "assets"}:
        return prefix
    return "other"


@dataclass(frozen=True)
class SkillUpdateCandidate:
    skill_id: str
    old_revision: int
    old_resolved_ref: str
    old_content_hash: str
    old_source: SkillSourceRef
    new_resolved_ref: str
    candidate_hash: str
    source: SkillSourceRef
    operation: Literal["update", "replace"]
    diff: SkillManifestDiff
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        validate_skill_id(self.skill_id)
        if type(self.old_revision) is not int or self.old_revision < 1:
            raise ValueError("candidate old_revision 必须大于 0")
        for label, value in (
            ("old_resolved_ref", self.old_resolved_ref),
            ("old_content_hash", self.old_content_hash),
            ("new_resolved_ref", self.new_resolved_ref),
            ("candidate_hash", self.candidate_hash),
        ):
            if type(value) is not str:
                raise TypeError(f"candidate {label} 必须是 string")
            if not value.strip():
                raise ValueError(f"candidate {label} 不能为空")
        if type(self.old_source) is not SkillSourceRef:
            raise TypeError("candidate old_source 类型无效")
        if type(self.source) is not SkillSourceRef:
            raise TypeError("candidate source 类型无效")
        if type(self.operation) is not str:
            raise TypeError("candidate operation 必须是 string")
        if self.operation not in {"update", "replace"}:
            raise ValueError("candidate operation 必须是 update|replace")
        if type(self.diff) is not SkillManifestDiff:
            raise TypeError("candidate diff 类型无效")
        _require_aware_datetime(self.created_at, "created_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "skill_id": self.skill_id,
            "old_revision": self.old_revision,
            "old_resolved_ref": self.old_resolved_ref,
            "old_content_hash": self.old_content_hash,
            "old_source": self.old_source.to_dict(),
            "new_resolved_ref": self.new_resolved_ref,
            "candidate_hash": self.candidate_hash,
            "source": self.source.to_dict(),
            "operation": self.operation,
            "diff": self.diff.to_dict(),
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SkillUpdateCandidate":
        _require_exact_keys(
            value,
            {
                "skill_id",
                "old_revision",
                "old_resolved_ref",
                "old_content_hash",
                "old_source",
                "new_resolved_ref",
                "candidate_hash",
                "source",
                "operation",
                "diff",
                "created_at",
            },
            "Skill update candidate",
        )
        source = value["source"]
        old_source = value["old_source"]
        diff = value["diff"]
        if (
            not isinstance(source, dict)
            or not isinstance(old_source, dict)
            or not isinstance(diff, dict)
        ):
            raise ValueError("Skill candidate source/diff 格式无效")
        return cls(
            skill_id=_require_str(value["skill_id"], "skill_id"),
            old_revision=_require_int(value["old_revision"], "old_revision"),
            old_resolved_ref=_require_str(
                value["old_resolved_ref"],
                "old_resolved_ref",
            ),
            old_content_hash=_require_str(
                value["old_content_hash"],
                "old_content_hash",
            ),
            old_source=SkillSourceRef.from_dict(old_source),
            new_resolved_ref=_require_str(
                value["new_resolved_ref"],
                "new_resolved_ref",
            ),
            candidate_hash=_require_str(
                value["candidate_hash"],
                "candidate_hash",
            ),
            source=SkillSourceRef.from_dict(source),
            operation=cast(
                Literal["update", "replace"],
                _require_str(value["operation"], "operation"),
            ),
            diff=SkillManifestDiff.from_dict(diff),
            created_at=_parse_datetime(value["created_at"]),
        )


@dataclass(frozen=True)
class SkillUpdateReport:
    candidate: SkillUpdateCandidate
    semantic_summary: str | None = None
    summary_error: str | None = None

    def __post_init__(self) -> None:
        if type(self.candidate) is not SkillUpdateCandidate:
            raise TypeError("Skill update report candidate 类型无效")
        if self.semantic_summary is None and self.summary_error is None:
            raise ValueError("Skill update report 必须包含概括或明确错误")
        if self.semantic_summary is not None and not self.semantic_summary.strip():
            raise ValueError("semantic_summary 不能为空")
        if self.summary_error is not None and not self.summary_error.strip():
            raise ValueError("summary_error 不能为空")

    def to_dict(self) -> dict[str, object]:
        return {
            "semantic_summary": self.semantic_summary,
            "summary_error": self.summary_error,
            "candidate": self.candidate.to_dict(),
        }


@dataclass(frozen=True)
class SkillInstallCandidate:
    skill_id: str
    description: str
    source: SkillSourceRef
    resolved_ref: str
    content_hash: str
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        validate_skill_id(self.skill_id)
        validate_skill_description(self.description)
        if type(self.source) is not SkillSourceRef:
            raise TypeError("install candidate source 类型无效")
        if type(self.resolved_ref) is not str or type(self.content_hash) is not str:
            raise TypeError("install candidate ref/hash 必须是 string")
        if not self.resolved_ref.strip() or not self.content_hash.strip():
            raise ValueError("install candidate ref/hash 不能为空")
        _require_aware_datetime(self.created_at, "created_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "skill_id": self.skill_id,
            "description": self.description,
            "source": self.source.to_dict(),
            "resolved_ref": self.resolved_ref,
            "content_hash": self.content_hash,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SkillInstallCandidate":
        _require_exact_keys(
            value,
            {
                "skill_id",
                "description",
                "source",
                "resolved_ref",
                "content_hash",
                "created_at",
            },
            "Skill install candidate",
        )
        source = value["source"]
        if not isinstance(source, dict):
            raise ValueError("install candidate source 格式无效")
        return cls(
            skill_id=_require_str(value["skill_id"], "skill_id"),
            description=_require_str(value["description"], "description"),
            source=SkillSourceRef.from_dict(source),
            resolved_ref=_require_str(value["resolved_ref"], "resolved_ref"),
            content_hash=_require_str(value["content_hash"], "content_hash"),
            created_at=_parse_datetime(value["created_at"]),
        )
