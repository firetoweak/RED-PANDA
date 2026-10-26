"""装配后的文件工具遵守给定目录与权限，并向模型提供相应能力。"""
from __future__ import annotations

from pathlib import Path
import unittest
from tempfile import TemporaryDirectory
from konden.assistant.assembly import build_assistant_assembly
from konden.config import AssistantConfig
from konden.llm.api import LLMCallResult, LLMResponse, LLMUsage
from konden.paths import KondenHome
from tests.fixtures.memory_journal import MemoryJournal
from konden.runtime.dispatcher import AttemptContext
from tests.fixtures.workspaces import workspace_record
from tests.session_scheduler import SettlingScheduler


async def no_host_settings(operation, *args):
    return "continue" if operation == "compact_boundary" else None


class CapturingLlm:
    async def chat(self, messages, model, *, tools=None):
        self.messages = messages
        self.tools = tools
        return LLMCallResult(LLMResponse(content="done"), LLMUsage(input_tokens=1, output_tokens=1))


class DirectWorkspaceTest(unittest.IsolatedAsyncioTestCase):
    async def test_session_writes_its_directory_and_sees_its_tools(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            home = KondenHome(root / "home")
            llm = CapturingLlm()
            assembly = await build_assistant_assembly(
                AssistantConfig(model_name="test", compact_threshold_tokens=200_000, llm=llm),
                lambda *_: None, MemoryJournal(), session_id="agent",
                workspace=workspace_record(workspace), home=home,
                scheduler_factory=SettlingScheduler, session_transport=no_host_settings,
            )
            try:
                context = AttemptContext("agent", "write", "attempt-write", 1)
                write = assembly.bindings["write_file"]
                self.assertIs(write.requires_authorization({"path": "note.txt"}), True)
                result = await write.handler(context, {"path": "note.txt", "content": "direct"})
                self.assertTrue(result["ok"])
                self.assertEqual((workspace / "note.txt").read_text(), "direct")
                result = await assembly.bindings["read_file"].handler(context, {"path": "note.txt"})
                self.assertTrue(result["ok"])
                result = await write.handler(context, {"path": str(root / "outside.txt"), "content": "outside"})
                self.assertFalse(result["ok"])
                self.assertFalse((root / "outside.txt").exists())
                self.assertIs(assembly.bindings["execute_command"].requires_authorization, True)
                await assembly.sessions.create("agent")
                await assembly.sessions.receive_user_message("agent", "hello", delivery_id="input")
                await assembly.scheduler.join()
                names = {tool["function"]["name"] for tool in llm.tools}
                self.assertTrue({"read_file", "write_file", "execute_command"} <= names)
                command = next(tool["function"] for tool in llm.tools if tool["function"]["name"] == "execute_command")
                self.assertNotIn("file_access", command["parameters"]["properties"])
                environment = next(message["content"] for message in llm.messages if "<environment>" in str(message["content"]))
                self.assertIn("文件没有回退。", environment)
                for skill in assembly.runtime.step_runner._decision_maker._skill_tools._catalog.bundled_skills:
                    self.assertIn(skill.id, str(llm.messages))
                    self.assertNotIn(skill.bundle.main_instructions, environment)
            finally:
                await assembly.scheduler.close()
