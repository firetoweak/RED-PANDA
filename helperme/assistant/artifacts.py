from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
from typing import Protocol
from uuid import uuid4

from helperme.assistant.asset_locations import inherited_drawers
from helperme.runtime.dispatcher import AttemptContext, ToolBinding


class ArtifactNotFoundError(LookupError):
    pass


class ArtifactOffsetOutOfRangeError(ValueError):
    pass


_ARTIFACT_ID_PATTERN = re.compile(r"^art_[0-9a-f]{32}$")


def is_valid_artifact_id(value: object) -> bool:
    return type(value) is str and _ARTIFACT_ID_PATTERN.fullmatch(value) is not None


def _validate_read_request(artifact_id: str, offset: int, limit: int) -> None:
    if not is_valid_artifact_id(artifact_id):
        raise ValueError("artifact_id 格式无效")
    if type(offset) is not int or offset < 0:
        raise ValueError("artifact offset 必须是非负 int")
    if type(limit) is not int or limit < 1:
        raise ValueError("artifact limit 必须是正 int")


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: str
    size_chars: int


@dataclass(frozen=True, slots=True)
class ArtifactChunk:
    artifact_id: str
    content: str
    offset: int
    next_offset: int | None
    total_chars: int

    @property
    def truncated(self) -> bool:
        return self.next_offset is not None


class ArtifactStore(Protocol):
    def save(self, content: str) -> ArtifactRef:
        ...

    def read(
        self,
        artifact_id: str,
        offset: int,
        limit: int,
    ) -> ArtifactChunk:
        ...


class ArtifactGateway(Protocol):
    def for_session(self, session_id: str) -> ArtifactStore:
        ...


class MemoryArtifactStore:
    def __init__(self) -> None:
        self.contents: dict[str, str] = {}

    def save(self, content: str) -> ArtifactRef:
        if type(content) is not str:
            raise TypeError("artifact content 必须是 str")
        artifact_id = f"art_{uuid4().hex}"
        self.contents[artifact_id] = content
        return ArtifactRef(artifact_id, len(content))

    def read(self, artifact_id: str, offset: int, limit: int) -> ArtifactChunk:
        _validate_read_request(artifact_id, offset, limit)
        if artifact_id not in self.contents:
            raise ArtifactNotFoundError(artifact_id)
        content = self.contents[artifact_id]
        if offset > len(content):
            raise ArtifactOffsetOutOfRangeError(
                f"offset={offset}, total_chars={len(content)}"
            )
        end = min(offset + limit, len(content))
        return ArtifactChunk(
            artifact_id=artifact_id,
            content=content[offset:end],
            offset=offset,
            next_offset=end if end < len(content) else None,
            total_chars=len(content),
        )


class MemoryArtifactGateway:
    """测试与默认决策器用的进程内抽屉，按 Session 隔离。"""

    def __init__(self) -> None:
        self._stores: dict[str, MemoryArtifactStore] = {}

    def for_session(self, session_id: str) -> MemoryArtifactStore:
        return self._stores.setdefault(session_id, MemoryArtifactStore())


class FileArtifactStore:
    def __init__(self, root: Path, inherited: tuple[Path, ...] = ()) -> None:
        self._root = root.resolve()
        self._inherited = inherited
        self._root.mkdir(parents=True, exist_ok=True)

    def save(self, content: str) -> ArtifactRef:
        if type(content) is not str:
            raise TypeError("artifact content 必须是 str")
        artifact_id = f"art_{uuid4().hex}"
        self._path(artifact_id).write_text(content, encoding="utf-8")
        return ArtifactRef(artifact_id, len(content))

    def read(self, artifact_id: str, offset: int, limit: int) -> ArtifactChunk:
        _validate_read_request(artifact_id, offset, limit)
        own = self._path(artifact_id)
        path = next(
            (candidate for candidate in (own, *(root / own.name for root in self._inherited))
             if candidate.is_file()),
            None,
        )
        if path is None:
            raise ArtifactNotFoundError(artifact_id)
        content = path.read_text(encoding="utf-8")
        if offset > len(content):
            raise ArtifactOffsetOutOfRangeError(
                f"offset={offset}, total_chars={len(content)}"
            )
        end = min(offset + limit, len(content))
        return ArtifactChunk(
            artifact_id=artifact_id,
            content=content[offset:end],
            offset=offset,
            next_offset=end if end < len(content) else None,
            total_chars=len(content),
        )

    def _path(self, artifact_id: str) -> Path:
        if not is_valid_artifact_id(artifact_id):
            raise ValueError("artifact_id 格式无效")
        return self._root / f"{artifact_id}.json"


class FileArtifactGateway:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._inherited: dict[str, tuple[Path, ...]] = {}

    def for_session(self, session_id: str) -> FileArtifactStore:
        drawer = sha256(session_id.encode("utf-8")).hexdigest()
        inherited = self._inherited.get(session_id)
        if inherited is None:
            inherited = inherited_drawers(self._root, session_id, "artifacts")
            self._inherited[session_id] = inherited
        return FileArtifactStore(self._root / drawer / "artifacts", inherited)


READ_ARTIFACT_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "read_artifact",
        "description": (
            "分页读取因长度限制而外置保存的完整工具结果。"
            "只能使用工具结果真实提供的 artifact_id；"
            "offset 是字符偏移，limit 最大为 3000。"
            "只能读取本对话中出现过的 artifact_id。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "artifact_id": {
                    "type": "string",
                    "pattern": r"^art_[0-9a-f]{32}$",
                },
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 3000,
                    "default": 3000,
                },
            },
            "required": ["artifact_id"],
        },
    },
}


def read_artifact_binding(gateway: ArtifactGateway) -> dict[str, ToolBinding]:
    async def handler(
        context: AttemptContext,
        arguments: Mapping[str, object],
    ) -> object:
        store = gateway.for_session(context.session_id)
        artifact_id = arguments.get("artifact_id")
        if not is_valid_artifact_id(artifact_id):
            return {
                "ok": False,
                "code": "INVALID_ARGUMENT",
                "error": "artifact_id 格式无效",
            }
        offset = arguments.get("offset", 0)
        limit = arguments.get("limit", 3000)
        if type(offset) is not int or offset < 0:
            return {
                "ok": False,
                "code": "INVALID_ARGUMENT",
                "error": "offset 必须是 >= 0 的整数",
            }
        if type(limit) is not int or not 1 <= limit <= 3000:
            return {
                "ok": False,
                "code": "INVALID_ARGUMENT",
                "error": "limit 必须是 1 到 3000 的整数",
            }
        try:
            chunk = store.read(artifact_id, offset, limit)
        except ArtifactNotFoundError:
            return {
                "ok": False,
                "code": "ARTIFACT_NOT_FOUND",
                "error": f"artifact 不存在或不属于本对话: {artifact_id}",
            }
        except ArtifactOffsetOutOfRangeError as exc:
            return {
                "ok": False,
                "code": "ARTIFACT_OFFSET_OUT_OF_RANGE",
                "error": str(exc),
            }
        return {
            "ok": True,
            "code": "ARTIFACT_READ",
            "data": {
                "artifact_id": chunk.artifact_id,
                "content": chunk.content,
                "offset": chunk.offset,
                "next_offset": chunk.next_offset,
                "total_chars": chunk.total_chars,
                "truncated": chunk.truncated,
            },
        }

    return {"read_artifact": ToolBinding(handler)}
