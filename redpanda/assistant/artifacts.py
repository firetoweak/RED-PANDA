from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
import sqlite3
from typing import Protocol
from uuid import uuid4

from redpanda.assistant.asset_locations import inherited_drawers


class ArtifactNotFoundError(LookupError):
    pass


class ArtifactOffsetOutOfRangeError(ValueError):
    pass


_ARTIFACT_ID_PATTERN = re.compile(r"^art_[0-9a-f]{32}$")
ARTIFACT_BLOCK_CHARS = 8_192


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

    def find(self, artifact_id: str, query: str, offset: int) -> int:
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

    def find(self, artifact_id: str, query: str, offset: int) -> int:
        _validate_read_request(artifact_id, offset, 1)
        if artifact_id not in self.contents:
            raise ArtifactNotFoundError(artifact_id)
        return self.contents[artifact_id].find(query, offset)


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
        with closing(sqlite3.connect(self._root / "contents.sqlite")) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY, size_chars INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS blocks (
                    artifact_id TEXT NOT NULL, block_index INTEGER NOT NULL,
                    content TEXT NOT NULL, PRIMARY KEY (artifact_id, block_index)
                ) WITHOUT ROWID;
            """)

    def save(self, content: str) -> ArtifactRef:
        if type(content) is not str:
            raise TypeError("artifact content 必须是 str")
        artifact_id = f"art_{uuid4().hex}"
        with closing(sqlite3.connect(self._root / "contents.sqlite")) as db, db:
            db.execute("INSERT INTO artifacts VALUES (?, ?)", (artifact_id, len(content)))
            db.executemany("INSERT INTO blocks VALUES (?, ?, ?)", (
                (artifact_id, index // ARTIFACT_BLOCK_CHARS, content[index:index + ARTIFACT_BLOCK_CHARS])
                for index in range(0, len(content), ARTIFACT_BLOCK_CHARS)
            ))
        return ArtifactRef(artifact_id, len(content))

    def read(self, artifact_id: str, offset: int, limit: int) -> ArtifactChunk:
        _validate_read_request(artifact_id, offset, limit)
        path, total = self._locate(artifact_id)
        if offset > total:
            raise ArtifactOffsetOutOfRangeError(f"offset={offset}, total_chars={total}")
        end = min(offset + limit, total)
        with closing(sqlite3.connect(path)) as db:
            rows = db.execute(
                "SELECT content FROM blocks WHERE artifact_id=? AND block_index>=? "
                "AND block_index<=? ORDER BY block_index",
                (artifact_id, offset // ARTIFACT_BLOCK_CHARS, (end - 1) // ARTIFACT_BLOCK_CHARS),
            )
            content = "".join(row[0] for row in rows)
        start = offset % ARTIFACT_BLOCK_CHARS
        content = content[start:start + end - offset]
        if len(content) != end - offset:
            raise ValueError("incomplete artifact blocks")
        return ArtifactChunk(
            artifact_id=artifact_id,
            content=content,
            offset=offset,
            next_offset=end if end < total else None,
            total_chars=total,
        )

    def _locate(self, artifact_id: str) -> tuple[Path, int]:
        if not is_valid_artifact_id(artifact_id):
            raise ValueError("artifact_id 格式无效")
        for root in (self._root, *self._inherited):
            path = root / "contents.sqlite"
            if not path.is_file():
                continue
            with closing(sqlite3.connect(path)) as db:
                row = db.execute("SELECT size_chars FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
            if row is not None:
                return path, row[0]
        raise ArtifactNotFoundError(artifact_id)

    def find(self, artifact_id: str, query: str, offset: int) -> int:
        _validate_read_request(artifact_id, offset, 1)
        path, total = self._locate(artifact_id)
        carry = ""
        position = offset
        expected_index = offset // ARTIFACT_BLOCK_CHARS
        with closing(sqlite3.connect(path)) as db:
            rows = db.execute(
                "SELECT block_index, content FROM blocks WHERE artifact_id=? AND block_index>=? ORDER BY block_index",
                (artifact_id, offset // ARTIFACT_BLOCK_CHARS),
            )
            for index, content in rows:
                if index != expected_index or len(content) != min(ARTIFACT_BLOCK_CHARS, total - index * ARTIFACT_BLOCK_CHARS):
                    raise ValueError("incomplete artifact blocks")
                expected_index += 1
                start = max(offset - index * ARTIFACT_BLOCK_CHARS, 0)
                part = content[start:]
                window = carry + part
                found = window.find(query)
                if found >= 0:
                    return position - len(carry) + found
                position += len(part)
                carry = window[-(len(query) - 1):] if len(query) > 1 else ""
        if position != total:
            raise ValueError("incomplete artifact blocks")
        return -1


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
