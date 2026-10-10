from __future__ import annotations

import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from redpanda.assistant.artifacts import MemoryArtifactGateway
from redpanda.assistant.assembly import build_assistant_assembly
from redpanda.assistant.context.projection import externalize_payload
from redpanda.assistant.context.prompt import (
    DEFAULT_ASSISTANT_PROMPT,
    SUBAGENT_PROMPT,
)
from redpanda.assistant.subagent.subagent import (
    CHILD_BUILTIN_TOOL_NAMES,
    REPORT_FACT,
    TASK_FACT,
    DelegateIntent,
    task_fact_arguments,
)
from redpanda.assistant.toolsets import LOAD_TOOLSET
from redpanda.assistant.work_plan import WORK_PLAN_TAG
from redpanda.config import AssistantConfig
from redpanda.llm.api import LLMCallResult, LLMResponse, LLMUsage
from redpanda.paths import RedPandaHome
from redpanda.runtime import MemoryJournal
from redpanda.skills.runtime import LOAD_SKILL, READ_SKILL_RESOURCE
from redpanda.tools.builtin import create_subagent_workspace_specs
from redpanda.tools.executor import RESERVED_KEYS
from tests.fixtures.workspaces import workspace_record


class DefaultAssistantPromptTests(unittest.TestCase):
    def test_guides_workspace_verification_after_file_writes(self):
        self.assertIn(
            "修改文件或执行命令产生文件变化后，用 get_changes 核对实际效果，"
            "并在最终汇报前确认仍保留的成果；不把外部修改或来源不明的改动算作自己完成。"
            "read_file 可核对当前内容，不能补出未知原值。文件变化不能代替功能测试。",
            DEFAULT_ASSISTANT_PROMPT,
        )


_ASCII_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

PROSE_VOCABULARY = frozenset(
    {
        "Agent",
        "Git",
        "RED",
        "PANDA",
        "Skill",
        "Toolset",
        "diff",
        "env",
        "false",
        "null",
        "true",
    }
)
"""prompt 正文里合法出现的英文散词和字面量，不指代任何工具或字段。

显式登记而不是过滤：prompt 里每出现一个新的 ASCII 词，都必须先判断它是
工具名、协议字段名，还是散词。漂移只有在这个判断被跳过时才会溜进去。
"""


CONDITIONAL_TOOL_NAMES = frozenset({
    LOAD_TOOLSET,
    LOAD_SKILL,
    READ_SKILL_RESOURCE,
    *(spec.name for spec in create_subagent_workspace_specs(None)),
})
"""按状态暴露的工具：没有可加载 Toolset、没有已装 Skill、没有 Host 传输时它们不在 schemas 里。

从定义处导入而不是写死字符串，改名仍然会被跟上；prompt 里提到它们是合法的，
因为 prompt 要负责让模型知道这些能力存在。
"""


TOOL_RESULT_FIELD_NAMES = frozenset({"content_complete"})
"""prompt 引用的工具返回值字段名。

这一条的保障比工具名弱，且弱得有理由：工具输入有 pydantic 模型，能从 schema
枚举；工具返回值是 handler 里的 dict 字面量，代码里没有任何声明可以对照。所以
这里只能拦住"prompt 提到一个从没人写过的字段"，拦不住"字段被改名"。
"""


class SilentLlm:
    async def chat(self, messages, model, *, tools=None):
        return LLMCallResult(
            LLMResponse(content="", calls=()),
            LLMUsage(input_tokens=0, output_tokens=0),
        )


def _ascii_tokens(text: str) -> set[str]:
    return set(_ASCII_TOKEN.findall(text))


def _schema_field_names(node: object) -> set[str]:
    names: set[str] = set()
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            names |= set(properties)
        for value in node.values():
            names |= _schema_field_names(value)
    elif isinstance(node, list):
        for item in node:
            names |= _schema_field_names(item)
    return names


def _externalized_meta_fields() -> set[str]:
    """从真实外置路径取字段名，而不是照抄一份常量。"""

    gateway = MemoryArtifactGateway()
    payload, _artifact_id = externalize_payload(
        {"ok": True, "code": "OK", "data": "x" * 64},
        gateway.for_session("prompt-vocabulary"),
        max_chars=8,
        preview_chars=4,
    )
    if not isinstance(payload, dict):
        raise TypeError("oversized payload must externalize to a dict")
    return set(payload) | set(payload["data"])


def _fact_type_tokens() -> set[str]:
    tokens: set[str] = set()
    for fact_type in (TASK_FACT, REPORT_FACT):
        tokens |= _ascii_tokens(fact_type)
    task = task_fact_arguments(DelegateIntent("command", "parent", "parent/sub-command", "task"))
    return tokens | set(task["data"])


class PromptVocabularyTests(unittest.IsolatedAsyncioTestCase):
    """prompt 提到的每个工具名和字段名都必须真实存在。

    这里锁的是一致性，不是措辞：随便改写句子都不会红，但一旦 prompt 指向一个
    已经改名或从未存在的工具/字段，就会红。模型没有办法发现这种漂移——它只会
    照着 prompt 去找一个不存在的东西。
    """

    async def asyncSetUp(self) -> None:
        self._directory = TemporaryDirectory()
        root = Path(self._directory.name)
        workspace = root / "workspace"
        workspace.mkdir()
        self._workspace = workspace_record(workspace)
        with (
            patch(
                "redpanda.assistant.assembly.RedPandaHome.default",
                return_value=RedPandaHome(root / ".redpanda"),
            ),
            patch(
                "redpanda.assistant.assembly.runtime_data_root",
                return_value=root / "runtime",
            ),
        ):
            self._assembly = await build_assistant_assembly(
                AssistantConfig(
                    model_name="test-model",
                    compact_threshold_tokens=200_000,
                    llm=SilentLlm(),
                ),
                lambda _session_id, _output_id, _text: None,
                MemoryJournal(),
                session_id="parent/sub-vocabulary",
                workspace=self._workspace,
            )

    async def asyncTearDown(self) -> None:
        await self._assembly.scheduler.close()
        self._directory.cleanup()

    def _exposed_schemas(self, session_id: str) -> list[dict[str, object]]:
        assembly = self._assembly
        decision = assembly.runtime.step_runner._decision_maker
        return [
            *assembly.surface.schemas(session_id),
            *decision._skill_tools.schemas(),
            *decision._management.schemas(session_id),
            *assembly.control.schemas(
                session_id,
                (),
                decision._management.control_names(session_id),
            ),
            *assembly.subagents.schemas(session_id),
        ]

    def _tool_names(self, session_id: str) -> set[str]:
        return {
            schema["function"]["name"]  # type: ignore[index]
            for schema in self._exposed_schemas(session_id)
        } | set(CONDITIONAL_TOOL_NAMES)

    def _known_names(self, session_id: str) -> set[str]:
        schemas = self._exposed_schemas(session_id)
        return (
            self._tool_names(session_id)
            | _schema_field_names(schemas)
            | set(RESERVED_KEYS)
            | set(TOOL_RESULT_FIELD_NAMES)
            | _externalized_meta_fields()
            | _fact_type_tokens()
            | {WORK_PLAN_TAG}
        )

    def test_default_prompt_names_only_real_tools_and_fields(self):
        unknown = (
            _ascii_tokens(DEFAULT_ASSISTANT_PROMPT)
            - PROSE_VOCABULARY
            - self._known_names("vocabulary")
        )
        self.assertEqual(
            unknown,
            set(),
            f"prompt 指向了不存在的工具名或字段名: {sorted(unknown)}",
        )

    def test_subagent_prompt_names_only_real_tools_and_fields(self):
        session_id = "parent/sub-vocabulary"
        self._assembly.subagents._parents[session_id] = "parent"
        unknown = (
            _ascii_tokens(SUBAGENT_PROMPT)
            - PROSE_VOCABULARY
            - self._known_names(session_id)
        )
        self.assertEqual(
            unknown,
            set(),
            f"子 Agent prompt 指向了不存在的工具名或字段名: {sorted(unknown)}",
        )

    def test_decision_prompt_tells_model_its_workspace(self):
        decision = self._assembly.runtime.step_runner._decision_maker
        prompt = decision.prompt_for(SimpleNamespace(session_id="vocabulary"))
        self.assertIn(f"工作区：{self._workspace.task_root}", prompt)

    def test_subagent_prompt_names_no_tool_it_cannot_call(self):
        """子 Session 的工具名单比主对话窄，prompt 不能许诺名单外的动作。"""

        mentioned = _ascii_tokens(SUBAGENT_PROMPT) & self._tool_names("vocabulary")
        self.assertEqual(
            mentioned - CHILD_BUILTIN_TOOL_NAMES - {LOAD_TOOLSET},
            set(),
            "子 Agent prompt 提到了它调不到的工具: "
            f"{sorted(mentioned - CHILD_BUILTIN_TOOL_NAMES - {LOAD_TOOLSET})}",
        )


if __name__ == "__main__":
    unittest.main()
