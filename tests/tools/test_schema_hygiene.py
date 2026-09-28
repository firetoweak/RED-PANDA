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

from pydantic import BaseModel, Field

from helperme.assistant.assembly import build_assistant_assembly
from helperme.config import AssistantConfig
from helperme.llm.types import LLMCallResult, LLMResponse, LLMUsage
from helperme.paths import HelperMeHome
from helperme.runtime import MemoryJournal
from helperme.tools.spec import PydanticParameters
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


class PydanticParametersTests(unittest.TestCase):
    """自动生成的噪音要压掉，作者刻意写的要留下。"""

    def test_auto_generated_titles_are_dropped(self):
        class Sample(BaseModel):
            path: str = Field(description="要读取的文件路径")
            max_depth: int = Field(default=1, description="深度限制")

        schema = PydanticParameters(Sample).schema()

        self.assertEqual(_keys_named(schema, "title"), [])

    def test_model_class_name_is_not_exposed(self):
        class GlobbyInput(BaseModel):
            pattern: str = Field(description="glob 模式")

        schema = PydanticParameters(GlobbyInput).schema()

        self.assertNotIn("title", schema)

    def test_deliberate_field_title_survives(self):
        """压掉的是"自动补的"，不是"作者写的"。

        用无差别遍历删除 title 也能让上面两条通过，但会连带删掉作者的意图。
        """

        class Sample(BaseModel):
            picked: int = Field(
                default=1,
                title="刻意写的标题",
                description="带显式 title 的字段",
            )

        schema = PydanticParameters(Sample).schema()

        self.assertEqual(
            _keys_named(schema, "title"),
            ["刻意写的标题"],
        )

    def test_docstring_does_not_leak_into_schema(self):
        class Documented(BaseModel):
            """这句话是写给 Python 读者的，不该进模型上下文。"""

            path: str = Field(description="路径")

        schema = PydanticParameters(Documented).schema()

        self.assertNotIn("description", schema)

    def test_nested_model_class_metadata_is_dropped(self):
        """嵌套模型的类名和 docstring 走 $defs，同样不该露出去。

        字段自己的 description 是写给模型的，必须留下。
        """

        class Nested(BaseModel):
            """嵌套模型的 docstring。"""

            value: str = Field(description="值")

        class Outer(BaseModel):
            nested: Nested

        schema = PydanticParameters(Outer).schema()

        self.assertEqual(_keys_named(schema, "title"), [])
        self.assertEqual(_keys_named(schema, "description"), ["值"])


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
                    "helperme.assistant.assembly.HelperMeHome.default",
                    return_value=HelperMeHome(root / ".helperme"),
                ),
                patch(
                    "helperme.assistant.assembly.runtime_data_root",
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

    async def test_no_tool_exposes_an_unregistered_title(self):
        offenders = {
            schema["function"]["name"]
            for schema in await self._exposed_schemas()
            if _keys_named(schema, "title")
        }

        self.assertEqual(
            offenders - TOOLS_WITH_DELIBERATE_TITLES,
            set(),
            f"这些工具的 schema 里有未登记的 title: {sorted(offenders)}",
        )

    async def test_no_parameters_object_carries_a_description(self):
        """参数对象自己的描述总是和 ToolSpec.description 重复。"""

        offenders = {
            schema["function"]["name"]
            for schema in await self._exposed_schemas()
            if "description" in schema["function"]["parameters"]
        }

        self.assertEqual(
            offenders,
            set(),
            f"这些工具的参数对象带了多余的 description: {sorted(offenders)}",
        )


if __name__ == "__main__":
    unittest.main()
