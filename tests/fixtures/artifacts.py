"""进程内 Artifact 存储替身，只供测试。"""
from __future__ import annotations

from uuid import uuid4

from redpanda.assistant.artifacts import (
    ArtifactChunk,
    ArtifactNotFoundError,
    ArtifactOffsetOutOfRangeError,
    ArtifactRef,
    _validate_read_request,
)


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
    """进程内抽屉，按 Session 隔离。"""

    def __init__(self) -> None:
        self._stores: dict[str, MemoryArtifactStore] = {}

    def for_session(self, session_id: str) -> MemoryArtifactStore:
        return self._stores.setdefault(session_id, MemoryArtifactStore())
