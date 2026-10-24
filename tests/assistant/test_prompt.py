from __future__ import annotations

import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from tests.fixtures.artifacts import MemoryArtifactGateway
from konden.assistant.agent_messages import MESSAGE_SCHEMAS
from konden.assistant.shared_memory import SHARED_MEMORY_SCHEMAS, SHARED_MEMORY_TAG
from konden.assistant.assembly import build_assistant_assembly
from konden.assistant.context.projection import externalize_payload
from konden.assistant.context.prompt import (
    DEFAULT_ASSISTANT_PROMPT,
    BOT_PROMPT,
    SUBAGENT_PROMPT,
    SUBAGENT_READ_ONLY_PROMPT,
)
from konden.assistant.subagent.subagent import (
    CHILD_BUILTIN_TOOL_NAMES,
    CLOSED_FACT,
    TASK_FACT,
    DelegateIntent,
    task_fact_arguments,
)
from konden.assistant.toolsets import LOAD_TOOLSET
from konden.assistant.work_plan import WORK_PLAN_TAG
from konden.skills.runtime import PROJECT_SKILL_PREFIX, PROJECT_SKILLS_DIRECTORY
from konden.config import AssistantConfig
from konden.llm.api import LLMCallResult, LLMResponse, LLMUsage
from konden.paths import KondenHome
from tests.fixtures.memory_journal import MemoryJournal
from konden.skills.runtime import LOAD_SKILL, READ_SKILL_RESOURCE
from konden.tools.builtin import create_subagent_workspace_specs
from konden.tools.executor import RESERVED_KEYS
from tests.fixtures.workspaces import workspace_record


_ASCII_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_PROMPT_REFERENCE = re.compile(
    r"\b[a-z]+(?:_[a-z0-9]+)+\b|(?<!`)`([A-Za-z_][A-Za-z0-9_]*)`(?!`)"
)
"""Explicit inline identifiers and snake_case names, independent of prose language."""


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


def _prompt_references(text: str) -> set[str]:
    return {match.group(1) or match.group(0) for match in _PROMPT_REFERENCE.finditer(text)}


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
    for fact_type in (TASK_FACT, CLOSED_FACT):
        tokens |= _ascii_tokens(fact_type)
    tokens |= _ascii_tokens(PROJECT_SKILL_PREFIX + "/".join(PROJECT_SKILLS_DIRECTORY))
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

        async def no_transport(*_args):
            raise AssertionError("prompt checks must not invoke host operations")

        with (
            patch(
                "konden.assistant.assembly.KondenHome.default",
                return_value=KondenHome(root / ".konden"),
            ),
            patch(
                "konden.assistant.assembly.runtime_data_root",
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
                session_transport=no_transport,
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
            *decision._cli_tools.schemas(),
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
            | {WORK_PLAN_TAG, SHARED_MEMORY_TAG}
        )

    def test_default_prompt_names_only_real_tools_and_fields(self):
        # Colleague and person messaging tools are assembled only for long-lived Agents.
        agent_schemas = [*MESSAGE_SCHEMAS, *SHARED_MEMORY_SCHEMAS]
        agent_references = {schema["function"]["name"] for schema in agent_schemas} | _schema_field_names(agent_schemas)
        for prompt, extra in ((DEFAULT_ASSISTANT_PROMPT, set()), (BOT_PROMPT, agent_references)):
            unknown = (
                _prompt_references(prompt)
                - self._known_names("vocabulary")
                - extra
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
            _prompt_references(SUBAGENT_PROMPT)
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
        self.assertIn(f"Working directory: {self._workspace.task_root}", prompt)

    def test_project_file_content_is_read_on_demand_instead_of_injected_into_system(self):
        decision = self._assembly.runtime.step_runner._decision_maker
        state = SimpleNamespace(session_id="vocabulary")
        root = Path(self._workspace.task_root)
        before = decision.prompt_for(state)
        (root / "AGENTS.md").write_text("PROJECT_AGENTS_SENTINEL", encoding="utf-8")
        (root / "CLAUDE.md").write_text("PROJECT_CLAUDE_SENTINEL", encoding="utf-8")
        self.assertEqual(decision.prompt_for(state), before)
        for item in decision._skill_tools._catalog.bundled_skills:
            self.assertNotIn(item.bundle.main_instructions, before)

    def test_subagent_prompt_names_no_tool_it_cannot_call(self):
        """子 Session 的工具名单比主对话窄，prompt 不能许诺名单外的动作。"""

        mentioned = _ascii_tokens(SUBAGENT_PROMPT) & self._tool_names("vocabulary")
        self.assertEqual(
            mentioned - CHILD_BUILTIN_TOOL_NAMES - {LOAD_TOOLSET},
            set(),
            "子 Agent prompt 提到了它调不到的工具: "
            f"{sorted(mentioned - CHILD_BUILTIN_TOOL_NAMES - {LOAD_TOOLSET})}",
        )


class SubagentProjectInstructionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_subagent_has_the_same_on_demand_knowledge_boundary(self):
        from datetime import datetime, timezone

        from konden.runtime.events import DeliveryIdentity, DomainFactCommitted, EventDraft

        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "workspace").mkdir()
            (root / "workspace" / "AGENTS.md").write_text("父工作区约定", encoding="utf-8")
            workspace = workspace_record(root / "workspace")
            home = KondenHome(root / ".konden")
            child = "parent/sub-delegate-1"

            journal = MemoryJournal()
            await journal.accept_delivery(EventDraft(
                event_id="task", session_id=child,
                payload=DomainFactCommitted("subagent.task", {
                    "task": "read", "parent_session_id": "parent",
                    "delegate_command_id": "delegate-1", "resolve_conflicts_of": None, "project": None,
                }, requests_decision=True),
                occurred_at=datetime.now(timezone.utc),
                delivery=DeliveryIdentity("subagent", "delegate-1:task"),
            ))
            with patch("konden.assistant.assembly.runtime_data_root", return_value=root / "runtime"):
                assembly = await build_assistant_assembly(
                    AssistantConfig(model_name="test-model", compact_threshold_tokens=200_000, llm=SilentLlm()),
                    lambda _session_id, _output_id, _text: None,
                    journal, session_id=child, workspace=workspace, home=home,
                )
            try:
                decision = assembly.runtime.step_runner._decision_maker
                state = SimpleNamespace(session_id=child)
                prompt = decision.prompt_for(state)
                self.assertTrue(prompt.startswith(SUBAGENT_READ_ONLY_PROMPT))
                self.assertNotIn("父工作区约定", prompt)
                self.assertIn(str(workspace.task_root), prompt)
                self.assertIn(LOAD_SKILL, assembly.subagents.tool_names(child))
            finally:
                await assembly.scheduler.close()


if __name__ == "__main__":
    unittest.main()
