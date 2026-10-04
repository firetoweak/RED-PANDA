from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Literal, cast


_CLI_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def validate_cli_id(cli_id: str) -> str:
    if not isinstance(cli_id, str) or not _CLI_ID_PATTERN.fullmatch(cli_id):
        raise ValueError(
            "CLI name 必须匹配 ^[a-z0-9][a-z0-9-]{0,63}$"
        )
    return cli_id


def validate_cli_description(description: str) -> str:
    if not isinstance(description, str) or not description.strip():
        raise ValueError("CLI description 不能为空")
    if description != description.strip():
        raise ValueError("CLI description 不能包含首尾空白")
    if "\n" in description or "\r" in description:
        raise ValueError("CLI description 必须是单行文本")
    if len(description) > 1_000:
        raise ValueError("CLI description 超出 1000 字符限制")
    return description


CliSourceKind = Literal["manifest"]


@dataclass(frozen=True)
class CliSourceRef:
    kind: CliSourceKind
    locator: str
    requested_version: str | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not str:
            raise TypeError("CLI source kind 必须是 string")
        if self.kind not in {"manifest"}:
            raise ValueError(f"不支持的 CLI source kind: {self.kind}")
        if type(self.locator) is not str:
            raise TypeError("CLI source locator 必须是 string")
        if not self.locator.strip():
            raise ValueError("CLI source locator 不能为空")
        if self.requested_version is not None:
            if type(self.requested_version) is not str:
                raise TypeError("CLI requested_version 必须是 string|null")
            if not self.requested_version:
                raise ValueError("CLI requested_version 不能为空")

    def to_dict(self) -> dict[str, str | None]:
        return {
            "kind": self.kind,
            "locator": self.locator,
            "requested_version": self.requested_version,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "CliSourceRef":
        _require_exact_keys(
            value,
            {"kind", "locator", "requested_version"},
            "CLI source",
        )
        return cls(
            kind=cast(CliSourceKind, _require_str(value["kind"], "kind")),
            locator=_require_str(value["locator"], "locator"),
            requested_version=_require_optional_str(
                value["requested_version"],
                "requested_version",
            ),
        )


@dataclass(frozen=True)
class CliHealth:
    help_ok: bool
    version_ok: bool
    help_mentions_json: bool
    checked_at: datetime

    def __post_init__(self) -> None:
        for label, value in (
            ("help_ok", self.help_ok),
            ("version_ok", self.version_ok),
            ("help_mentions_json", self.help_mentions_json),
        ):
            if type(value) is not bool:
                raise TypeError(f"CLI health {label} 必须是 bool")
        _require_aware_datetime(self.checked_at, "checked_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "help_ok": self.help_ok,
            "version_ok": self.version_ok,
            "help_mentions_json": self.help_mentions_json,
            "checked_at": self.checked_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "CliHealth":
        _require_exact_keys(
            value,
            {"help_ok", "version_ok", "help_mentions_json", "checked_at"},
            "CLI health",
        )
        return cls(
            help_ok=_require_bool(value["help_ok"], "help_ok"),
            version_ok=_require_bool(value["version_ok"], "version_ok"),
            help_mentions_json=_require_bool(
                value["help_mentions_json"],
                "help_mentions_json",
            ),
            checked_at=_parse_datetime(value["checked_at"]),
        )


@dataclass(frozen=True)
class CliRecord:
    name: str
    description: str
    source: CliSourceRef
    version: str | None
    resolved_path: str | None
    health: CliHealth | None
    revision: int = 1
    created_at: datetime = field(default_factory=lambda: utc_now())
    updated_at: datetime = field(default_factory=lambda: utc_now())

    def __post_init__(self) -> None:
        validate_cli_id(self.name)
        validate_cli_description(self.description)
        if type(self.source) is not CliSourceRef:
            raise TypeError("CLI record source 必须是 CliSourceRef")
        if self.version is not None:
            if type(self.version) is not str:
                raise TypeError("CLI record version 必须是 string|null")
            if not self.version.strip():
                raise ValueError("CLI record version 不能为空串")
        if self.resolved_path is not None:
            if type(self.resolved_path) is not str:
                raise TypeError("CLI record resolved_path 必须是 string|null")
            if not self.resolved_path.strip():
                raise ValueError("CLI record resolved_path 不能为空串")
        if self.health is not None and type(self.health) is not CliHealth:
            raise TypeError("CLI record health 必须是 CliHealth|null")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("CLI revision 必须大于 0")
        _require_aware_datetime(self.created_at, "created_at")
        _require_aware_datetime(self.updated_at, "updated_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "source": self.source.to_dict(),
            "version": self.version,
            "resolved_path": self.resolved_path,
            "health": None if self.health is None else self.health.to_dict(),
            "revision": self.revision,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "CliRecord":
        _require_exact_keys(
            value,
            {
                "name",
                "description",
                "source",
                "version",
                "resolved_path",
                "health",
                "revision",
                "created_at",
                "updated_at",
            },
            "CLI record",
        )
        raw_source = value["source"]
        if not isinstance(raw_source, dict):
            raise ValueError("CLI record source 必须是 object")
        raw_health = value["health"]
        if raw_health is not None and not isinstance(raw_health, dict):
            raise ValueError("CLI record health 必须是 object|null")
        return cls(
            name=validate_cli_id(_require_str(value["name"], "name")),
            description=validate_cli_description(
                _require_str(value["description"], "description")
            ),
            source=CliSourceRef.from_dict(raw_source),
            version=_require_optional_str(value["version"], "version"),
            resolved_path=_require_optional_str(
                value["resolved_path"],
                "resolved_path",
            ),
            health=(
                None if raw_health is None else CliHealth.from_dict(raw_health)
            ),
            revision=_require_int(value["revision"], "revision"),
            created_at=_parse_datetime(value["created_at"]),
            updated_at=_parse_datetime(value["updated_at"]),
        )


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
