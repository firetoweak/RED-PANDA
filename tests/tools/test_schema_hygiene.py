"""工具 schema 里不该出现只有 Python 读者才用得上的东西。

模型每次请求都要付整个工具面的钱，而 pydantic 默认会把字段名换个大小写塞进
`title`、把类 docstring 塞进 `description`。这些对模型是零信息：property key
已经在那里了，参数对象的描述也和 ToolSpec.description 重复。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from redpanda.assistant.assembly import build_assistant_assembly
from redpanda.config import AssistantConfig
from redpanda.llm.api import LLMCallResult, LLMResponse, LLMUsage
from redpanda.paths import RedPandaHome
from redpanda.runtime import MemoryJournal
from tests.fixtures.workspaces import workspace_record


TOOLS_WITH_DELIBERATE_TITLES: frozenset[str] = frozenset()
"""允许出现 `title` 的工具。

`Field(title=...)` 是作者刻意写的，生成器不会压掉它。但它对模型的价值需要
逐个论证——property key 通常已经说清楚了——所以默认一个都不许，要加先登记。
"""


class SilentLlm:
    async def chat(self, messages, model, *, tools=None):
        return LLMCallResult(
            LLMResponse(content="", calls=()),
            LLMUsage(input_tokens=0, output_tokens=0),
        )


def _keys_named(node: object, wanted: str) -> list[object]:
    found: list[object] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == wanted:
                found.append(value)
            found.extend(_keys_named(value, wanted))
    elif isinstance(node, list):
        for item in node:
            found.extend(_keys_named(item, wanted))
    return found


class ExposedToolSurfaceTests(unittest.IsolatedAsyncioTestCase):
    """整个默认工具面扫一遍，防止某个工具单独跑偏。"""

    async def _exposed_schemas(
        self,
        session_id: str = "hygiene",
    ) -> list[dict[str, Any]]:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
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
                assembly = await build_assistant_assembly(
                    AssistantConfig(
                        model_name="test-model",
                        compact_threshold_tokens=200_000,
                        llm=SilentLlm(),
                    ),
                    lambda _session_id, _output_id, _text: None,
                    MemoryJournal(),
                    session_id=session_id,
                    workspace=workspace_record(workspace),
                )
                try:
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
                finally:
                    await assembly.scheduler.close()

    async def test_exposed_tool_schemas_carry_no_generated_noise(self):
        """未登记的 title、参数对象自带的 description 都不该进模型上下文。"""

        schemas = await self._exposed_schemas()
        titled = {
            schema["function"]["name"]
            for schema in schemas
            if _keys_named(schema, "title")
        }
        described = {
            schema["function"]["name"]
            for schema in schemas
            if "description" in schema["function"]["parameters"]
        }

        self.assertEqual(
            titled - TOOLS_WITH_DELIBERATE_TITLES,
            set(),
            f"这些工具的 schema 里有未登记的 title: {sorted(titled)}",
        )
        self.assertEqual(
            described,
            set(),
            f"这些工具的参数对象带了多余的 description: {sorted(described)}",
        )


if __name__ == "__main__":
    unittest.main()
