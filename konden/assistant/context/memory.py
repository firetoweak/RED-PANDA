"""Load current Bot constraints at the request tail, outside conversation history."""
from dataclasses import dataclass
import json

from konden.assistant.memory import MEMORY_RULES
from konden.assistant.shared_memory import SharedMemoryStore
from konden.assistant.personal_memory import PersonalMemoryStore


MEMORY_TAG = "working_memory"


@dataclass(frozen=True, slots=True)
class MemoryContext:
    shared: SharedMemoryStore
    personal: PersonalMemoryStore

    def messages(self):
        data = {
            "shared": self.shared.read().model_dump(),
            "personal": self.personal.read().model_dump(),
            "notice": "以下是本次判断适用的当前工作约束：shared 适用于所有 Bot，personal 仅适用于你。"
            "当前正文替代旧版本。共同约束不在个人记忆中重复保存，个人例外须有用户明确依据。"
            + MEMORY_RULES,
        }
        return [{"role": "user", "content": f"<{MEMORY_TAG}>\n"
                 + json.dumps(data, ensure_ascii=False) + f"\n</{MEMORY_TAG}>"}]
