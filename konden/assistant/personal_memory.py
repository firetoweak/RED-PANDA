"""A Bot's own small set of working constraints, independent of its projects."""
from __future__ import annotations

from hashlib import sha256

from pydantic import Field

from konden.assistant.memory import MEMORY_RULES, Memory, MemoryStore, UpdateMemory, memory_bindings


READ_PERSONAL_MEMORY = "read_personal_memory"
UPDATE_PERSONAL_MEMORY = "update_personal_memory"
MAX_CONTENT_CHARS = 200


class PersonalMemory(Memory):
    content: str = Field(max_length=MAX_CONTENT_CHARS)


class UpdatePersonalMemory(UpdateMemory):
    version: int = Field(ge=0, description="你最近读到的个人记忆版本。")
    content: str = Field(
        max_length=MAX_CONTENT_CHARS,
        description="整理后的完整正文，最多200字，标点、空白和换行也计数；空字符串表示清空。",
    )


PERSONAL_MEMORY_SCHEMAS = [
    {"type": "function", "function": {
        "name": READ_PERSONAL_MEMORY,
        "description": "查阅仅适用于你自己的个人工作约束，返回当前完整正文、版本和修改者。不接受参数。",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": UPDATE_PERSONAL_MEMORY,
        "description": (
            "修改仅适用于你自己的工作约束，只用于与你自己的职责有关、其他 Bot 不需要遵守的约束，"
            "或用户明确说只对你适用的例外。适用于团队的内容写共同记忆，同事都能看到，不需要各写一份。"
            "先读当前正文，再整理为完整的新正文，最多200字，可为空。"
            + MEMORY_RULES
            + "共同记忆已有的约束不重复保存；与共同约束冲突的例外须有用户明确依据。"
            "不能读写其他 Bot 的个人记忆。超限不会截断或保存。"
            "若返回 PERSONAL_MEMORY_CHANGED，先阅读返回的最新正文，再调整后重试。"
        ),
        "parameters": UpdatePersonalMemory.model_json_schema(),
    }},
]


class PersonalMemoryStore(MemoryStore):
    record_type = PersonalMemory
    label = "个人记忆"
    code = "PERSONAL_MEMORY"
    limit = MAX_CONTENT_CHARS


def personal_memory_store(home, agent):
    # The permanent Bot directory identifies its owner, including after a rename.
    owner = sha256(str(agent.directory).encode("utf-8")).hexdigest()
    return PersonalMemoryStore(home.state_root / "personal_memory" / f"{owner}.sqlite")


def personal_memory_bindings(store: PersonalMemoryStore, author: str):
    return memory_bindings(store, author, READ_PERSONAL_MEMORY, UPDATE_PERSONAL_MEMORY, UpdatePersonalMemory)
