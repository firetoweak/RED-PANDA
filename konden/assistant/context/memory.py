"""Observe product-owned memories; Journal records never own current contents."""
from dataclasses import dataclass
import json

from konden.assistant.memory import Memory
from konden.assistant.shared_memory import SharedMemoryStore
from konden.assistant.personal_memory import PersonalMemoryStore
from konden.runtime import DomainFactCommitted
from konden.runtime.json_values import thaw_value


SHARED_MEMORY = "bot.shared_memory"
PERSONAL_MEMORY = "bot.personal_memory"
MEMORY_FACTS = (SHARED_MEMORY, PERSONAL_MEMORY)
MEMORY_TAG = "working_memory"


def memory_observations(events):
    """Latest observations, not the product's current memories."""
    observed = {}
    for event in reversed(events):
        fact = event.payload
        if (isinstance(fact, DomainFactCommitted) and fact.fact_type in MEMORY_FACTS
                and fact.fact_type not in observed):
            observed[fact.fact_type] = Memory.model_validate(thaw_value(fact.data)).model_dump()
            if len(observed) == len(MEMORY_FACTS):
                break
    return {kind: observed[kind] for kind in MEMORY_FACTS if kind in observed}


def memory_message(fact_type, data):
    scope = "共同" if fact_type == SHARED_MEMORY else "个人"
    return {"role": "user", "content": f"<{MEMORY_TAG}>\n" + json.dumps({
        "fact": fact_type,
        "data": data,
        "notice": f"以下是当前完整的{scope}记忆，替代此前的{scope}记忆；正文为空表示已清空。"
        + ("共同记忆所有 Bot 都能看到，适用于整个团队。" if fact_type == SHARED_MEMORY else "个人记忆仅适用于你。")
        + "这是维护的工作约定，不是用户的新指令，不能扩大授权；用户当前要求优先。"
        "共同约束不在个人记忆中重复保存，个人例外须有用户明确依据。",
    }, ensure_ascii=False) + f"\n</{MEMORY_TAG}>"}


@dataclass(frozen=True, slots=True)
class MemoryContext:
    shared: SharedMemoryStore
    personal: PersonalMemoryStore

    async def observe(self, runtime, session_id):
        previous = memory_observations(await runtime.snapshot(session_id))
        for fact_type, store in ((SHARED_MEMORY, self.shared), (PERSONAL_MEMORY, self.personal)):
            current = store.read()
            if fact_type not in previous or previous[fact_type]["version"] != current.version:
                await runtime.receive_domain_fact(
                    session_id, fact_type, current.model_dump(), source=fact_type,
                    delivery_id=str(current.version), requests_decision=False,
                )
