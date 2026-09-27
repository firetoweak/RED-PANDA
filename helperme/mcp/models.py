from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import re
from typing import Any, Mapping
from urllib.parse import urlparse


_SERVER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# Streamable HTTP 把「URL query 整段」当作单个 Secret 抽取时的逻辑名。
QUERY_SECRET_KEY = "__url_query__"


def validate_server_id(server_id: str) -> str:
    if type(server_id) is not str or not _SERVER_ID_PATTERN.fullmatch(server_id):
        raise ValueError(
            "server id 必须匹配 "
            "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
        )
    return server_id


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TransportKind(str, Enum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"


class RuntimeAvailability(str, Enum):
    UNKNOWN = "unknown"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class StdioTransportConfig:
    command: str
    args: tuple[str, ...] = ()
    cwd: str | None = None
    env_refs: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.command) is not str:
            raise TypeError("stdio command 必须是 string")
        if not self.command.strip():
            raise ValueError("stdio command 不能为空")
        if type(self.args) is not tuple or any(
            type(argument) is not str for argument in self.args
        ):
            raise TypeError("stdio args 必须是 string tuple")
        if self.cwd is not None and type(self.cwd) is not str:
            raise TypeError("stdio cwd 必须是 string|null")
        if not isinstance(self.env_refs, Mapping) or any(
            type(key) is not str or type(value) is not str
            for key, value in self.env_refs.items()
        ):
            raise TypeError("stdio env_refs 必须是 string mapping")
        object.__setattr__(self, "env_refs", dict(self.env_refs))


@dataclass(frozen=True)
class StreamableHttpTransportConfig:
    url: str
    header_refs: Mapping[str, str] = field(default_factory=dict)
    query_refs: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if type(self.url) is not str:
            raise TypeError("streamable_http url 必须是 string")
        if not self.url.strip():
            raise ValueError("streamable_http url 不能为空")
        parsed = urlparse(self.url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme == "http":
            if host not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("非本机 HTTP MCP 地址必须使用 HTTPS")
        elif parsed.scheme != "https":
            raise ValueError("streamable_http url 必须是 http(s)")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds 必须大于 0")
        if not isinstance(self.header_refs, Mapping) or any(
            type(key) is not str or type(value) is not str
            for key, value in self.header_refs.items()
        ):
            raise TypeError("header_refs 必须是 string mapping")
        if not isinstance(self.query_refs, Mapping) or any(
            type(key) is not str or type(value) is not str
            for key, value in self.query_refs.items()
        ):
            raise TypeError("query_refs 必须是 string mapping")
        object.__setattr__(self, "header_refs", dict(self.header_refs))
        object.__setattr__(self, "query_refs", dict(self.query_refs))


TransportConfig = StdioTransportConfig | StreamableHttpTransportConfig


@dataclass(frozen=True)
class McpServerRecord:
    id: str
    display_name: str
    description: str
    transport: TransportKind
    transport_config: TransportConfig
    enabled: bool = False
    read_only: bool = False
    revision: int = 1
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    last_status: RuntimeAvailability = RuntimeAvailability.UNKNOWN
    last_checked_at: datetime | None = None
    last_error_summary: str = ""

    def __post_init__(self) -> None:
        validate_server_id(self.id)
        if type(self.display_name) is not str:
            raise TypeError("display_name 必须是 string")
        if not self.display_name.strip():
            raise ValueError("display_name 不能为空")
        if type(self.description) is not str:
            raise TypeError("description 必须是 string")
        if type(self.enabled) is not bool:
            raise TypeError("enabled 必须是 bool")
        if type(self.read_only) is not bool:
            raise TypeError("read_only 必须是 bool")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("revision 必须大于 0")
        for label, value in (
            ("created_at", self.created_at),
            ("updated_at", self.updated_at),
        ):
            if type(value) is not datetime or value.tzinfo is None:
                raise TypeError(f"{label} 必须是带时区的 datetime")
        if self.transport is TransportKind.STDIO:
            if not isinstance(self.transport_config, StdioTransportConfig):
                raise ValueError("stdio transport_config 类型不匹配")
        elif self.transport is TransportKind.STREAMABLE_HTTP:
            if not isinstance(
                self.transport_config,
                StreamableHttpTransportConfig,
            ):
                raise ValueError("streamable_http transport_config 类型不匹配")
        else:
            raise ValueError(f"不支持的 transport: {self.transport}")
        if not isinstance(self.last_status, RuntimeAvailability):
            raise TypeError("last_status 必须是 RuntimeAvailability")
        if self.last_checked_at is not None and (
            type(self.last_checked_at) is not datetime
            or self.last_checked_at.tzinfo is None
        ):
            raise TypeError("last_checked_at 必须是带时区的 datetime|null")
        if type(self.last_error_summary) is not str:
            raise TypeError("last_error_summary 必须是 string")

    @property
    def toolset_id(self) -> str:
        return f"mcp:{self.id}"

    @property
    def credential_refs(self) -> dict[str, str]:
        config = self.transport_config
        if isinstance(config, StdioTransportConfig):
            return dict(config.env_refs)
        return {**dict(config.header_refs), **dict(config.query_refs)}

    def to_dict(self) -> dict[str, Any]:
        config = self.transport_config
        if isinstance(config, StdioTransportConfig):
            transport_config = {
                "command": config.command,
                "args": list(config.args),
                "cwd": config.cwd,
                "env_refs": dict(config.env_refs),
            }
        else:
            transport_config = {
                "url": config.url,
                "header_refs": dict(config.header_refs),
                "query_refs": dict(config.query_refs),
                "timeout_seconds": config.timeout_seconds,
            }
        return {
            "id": self.id,
            "display_name": self.display_name,
            "description": self.description,
            "transport": self.transport.value,
            "transport_config": transport_config,
            "enabled": self.enabled,
            "read_only": self.read_only,
            "revision": self.revision,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "last_status": self.last_status.value,
            "last_checked_at": (
                self.last_checked_at.isoformat()
                if self.last_checked_at is not None
                else None
            ),
            "last_error_summary": self.last_error_summary,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "McpServerRecord":
        _require_exact_keys(
            payload,
            {
                "id",
                "display_name",
                "description",
                "transport",
                "transport_config",
                "enabled",
                "read_only",
                "revision",
                "created_at",
                "updated_at",
                "last_status",
                "last_checked_at",
                "last_error_summary",
            },
            "MCP server record",
        )
        transport = TransportKind(_require_str(payload["transport"], "transport"))
        raw_config = payload["transport_config"]
        if not isinstance(raw_config, Mapping):
            raise ValueError("transport_config 必须是 object")
        if transport is TransportKind.STDIO:
            _require_exact_keys(
                raw_config,
                {"command", "args", "cwd", "env_refs"},
                "stdio transport_config",
            )
            transport_config: TransportConfig = StdioTransportConfig(
                command=_require_str(raw_config["command"], "command"),
                args=_require_string_tuple(raw_config["args"], "args"),
                cwd=_require_optional_str(raw_config["cwd"], "cwd"),
                env_refs=_require_string_map(raw_config["env_refs"], "env_refs"),
            )
        else:
            _require_exact_keys(
                raw_config,
                {"url", "header_refs", "query_refs", "timeout_seconds"},
                "streamable_http transport_config",
            )
            timeout = raw_config["timeout_seconds"]
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise ValueError("timeout_seconds 必须是 number")
            transport_config = StreamableHttpTransportConfig(
                url=_require_str(raw_config["url"], "url"),
                header_refs=_require_string_map(
                    raw_config["header_refs"],
                    "header_refs",
                ),
                query_refs=_require_string_map(
                    raw_config["query_refs"],
                    "query_refs",
                ),
                timeout_seconds=float(timeout),
            )
        enabled = payload["enabled"]
        read_only = payload["read_only"]
        revision = payload["revision"]
        if type(enabled) is not bool:
            raise ValueError("enabled 必须是 bool")
        if type(read_only) is not bool:
            raise ValueError("read_only 必须是 bool")
        if type(revision) is not int:
            raise ValueError("revision 必须是 int")
        return cls(
            id=_require_str(payload["id"], "id"),
            display_name=_require_str(payload["display_name"], "display_name"),
            description=_require_str(payload["description"], "description"),
            transport=transport,
            transport_config=transport_config,
            enabled=enabled,
            read_only=read_only,
            revision=revision,
            created_at=_parse_datetime(payload["created_at"]),
            updated_at=_parse_datetime(payload["updated_at"]),
            last_status=RuntimeAvailability(payload["last_status"]),
            last_checked_at=(
                _parse_datetime(payload["last_checked_at"])
                if payload["last_checked_at"] is not None
                else None
            ),
            last_error_summary=_require_str(
                payload["last_error_summary"],
                "last_error_summary",
            ),
        )


def _require_exact_keys(
    value: Mapping[str, Any],
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


def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} 必须是 string")
    return value


def _require_optional_str(value: Any, label: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{label} 必须是 string|null")
    return value


def _require_string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise ValueError(f"{label} 必须是 string array")
    return tuple(value)


def _require_string_map(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise ValueError(f"{label} 必须是 string:string object")
    return dict(value)


@dataclass
class McpServerRuntimeState:
    status: RuntimeAvailability = RuntimeAvailability.UNKNOWN
    negotiated_version: str | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    last_error_summary: str | None = None
    last_checked_at: datetime | None = None
    tools: tuple[dict[str, Any], ...] = ()

    def mark_available(
        self,
        *,
        negotiated_version: str | None,
        capabilities: Mapping[str, Any] | None = None,
    ) -> None:
        self.status = RuntimeAvailability.AVAILABLE
        self.negotiated_version = negotiated_version
        self.capabilities = dict(
            {} if capabilities is None else capabilities
        )
        self.last_error_summary = None
        self.last_checked_at = utc_now()

    def mark_unavailable(self, error_summary: str) -> None:
        self.tools = ()
        self.status = RuntimeAvailability.UNAVAILABLE
        self.negotiated_version = None
        self.capabilities = {}
        self.last_error_summary = sanitize_error_summary(error_summary)
        self.last_checked_at = utc_now()

    def to_dict(self) -> dict[str, Any]:
        return {
            "tools": list(self.tools),
            "status": self.status.value,
            "negotiated_version": self.negotiated_version,
            "capabilities": dict(self.capabilities),
            "last_error_summary": self.last_error_summary,
            "last_checked_at": (
                self.last_checked_at.isoformat()
                if self.last_checked_at is not None
                else None
            ),
        }


def sanitize_error_summary(
    message: str,
    *,
    secret_values: tuple[str, ...] = (),
    limit: int = 240,
) -> str:
    text = " ".join(str(message).split())
    for secret in sorted(
        (value for value in secret_values if value),
        key=len,
        reverse=True,
    ):
        text = text.replace(secret, "***")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _parse_datetime(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("datetime 必须是非空 ISO 8601 字符串")
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
