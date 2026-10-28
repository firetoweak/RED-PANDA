"""Team-wide working constraints, loaded independently of conversation history."""
from __future__ import annotations

from pydantic import Field

from konden.assistant.memory import MEMORY_RULES, Memory, MemoryStore, UpdateMemory, memory_bindings


READ_SHARED_MEMORY = "read_shared_memory"
UPDATE_SHARED_MEMORY = "update_shared_memory"
MAX_CONTENT_CHARS = 500


class SharedMemory(Memory):
    content: str = Field(max_length=MAX_CONTENT_CHARS)


class UpdateSharedMemory(UpdateMemory):
    version: int = Field(ge=0, description="你最近读到的共同记忆版本。")
    content: str = Field(
        max_length=MAX_CONTENT_CHARS,
        description="整理后的完整正文，最多500字，标点、空白和换行也计数；空字符串表示清空。",
    )


SHARED_MEMORY_SCHEMAS = [
    {"type": "function", "function": {
        "name": READ_SHARED_MEMORY,
        "description": "查阅适用于所有 Bot 的共同工作约束，返回当前完整正文、版本和修改者。不接受参数。",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": UPDATE_SHARED_MEMORY,
        "description": (
            "修改所有 Bot 共享的工作约束。所有 Bot 都会看到同一份正文，写一次即可供整个团队共同遵守。"
            "用户的偏好、团队约定、需要同事共同遵守的要求写在这里；"
            "需要所有同事遵守时，写入共同记忆即可，不必逐个通知同事各自记录。"
            "不确定是否只适用于你时，写共同记忆。先读当前正文，再整理为完整的新正文，最多500字，可为空。"
            + MEMORY_RULES
            + "仅适用于你自己的约束使用个人记忆。超限不会截断或保存。"
            "若返回 SHARED_MEMORY_CHANGED，先阅读返回的最新正文，再调整后重试。"
        ),
        "parameters": UpdateSharedMemory.model_json_schema(),
    }},
]


class SharedMemoryStore(MemoryStore):
    record_type = SharedMemory
    table = "shared_memory"
    label = "共同记忆"
    code = "SHARED_MEMORY"
    limit = MAX_CONTENT_CHARS


def shared_memory_bindings(store: SharedMemoryStore, author: str):
    return memory_bindings(store, author, READ_SHARED_MEMORY, UPDATE_SHARED_MEMORY, UpdateSharedMemory)
