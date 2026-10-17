from __future__ import annotations

import asyncio
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch
import pytest

from redpanda.assistant.assembly import build_assistant_assembly
from redpanda.config import AssistantConfig
from redpanda.llm.api import LLMCallResult, LLMResponse, LLMUsage
from redpanda.paths import RedPandaHome
from redpanda.runtime import DecisionCancelled, MemoryJournal, SqliteJournal, StepCommitted
from redpanda.runtime.dispatcher import AttemptContext
from redpanda.filesystem import operation_id
from tests.fixtures.workspaces import workspace_record
from tests.session_scheduler import SettlingScheduler


@pytest.fixture(autouse=True)
def version_backend():
    # 本层验证装配；真实 VFS 文件恢复在 process 层。
    async def execute(self, identity, callback):
        return await callback()
    with (
        patch("redpanda.filesystem.operations.WorkspaceFiles.record", return_value="a" * 64),
        patch("redpanda.filesystem.operations.WorkspaceFiles.execute", execute),
        patch("redpanda.filesystem.operations.WorkspaceFiles.native_path", lambda self, path: path),
        patch("redpanda.filesystem.operations.WorkspaceFiles.logical_path", lambda self, path: path),
    ):
        yield



class CapturingLlm:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    async def chat(self, messages, model, *, tools=None):
        self.requests.append(
            {
                "projector": "model-context/v1",
                "model": model,
                "messages": deepcopy(messages),
                "tools": deepcopy(tools),
            }
        )
        return LLMCallResult(
            LLMResponse(content="done", calls=()),
            LLMUsage(input_tokens=1, output_tokens=1),
        )


class StreamingLlm:
    async def chat(
        self,
        _messages,
        _model,
        *,
        tools=None,
        on_content_delta=None,
        on_reasoning_delta=None,
    ):
        self.tools = tools
        await on_content_delta("hel")
        await on_content_delta("lo")
        return LLMCallResult(
            LLMResponse(content="hello"),
            LLMUsage(input_tokens=1, output_tokens=1),
        )


class BlockingStreamingLlm:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def chat(
        self,
        _messages,
        _model,
        *,
        tools=None,
        on_content_delta=None,
        on_reasoning_delta=None,
    ):
        self.tools = tools
        await on_content_delta("partial")
        self.started.set()
        await asyncio.Event().wait()



class AssemblyWiringTest(unittest.IsolatedAsyncioTestCase):
    async def test_get_changes_binds_current_session_operations_by_default(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            changes = AsyncMock(return_value={"ok": True, "code": "CHANGES_READ", "scope": "session",
                "changes": [], "content_complete": True, "truncated": False, "next_offset": None})
            journal_path = root / "sessions" / ("a" * 64) / "journal.sqlite"
            journal_path.parent.mkdir(parents=True)
            journal = SqliteJournal(journal_path)
            await journal.create_session("entry")
            with (
                patch("redpanda.assistant.assembly.RedPandaHome.default", return_value=RedPandaHome(root / "home")),
                patch("redpanda.assistant.assembly.runtime_data_root", return_value=root / "runtime"),
                patch("redpanda.filesystem.operations.WorkspaceFiles.changes", changes),
            ):
                assembly = await build_assistant_assembly(
                    AssistantConfig(model_name="test", compact_threshold_tokens=200_000, llm=CapturingLlm()),
                    lambda *_: None, journal, session_id="entry", workspace=workspace_record(workspace),
                )
                try:
                    await assembly.runtime.receive_user_message("entry", "inspect", delivery_id="input")
                    step = (await assembly.runtime.advance("entry")).step
                    context = AttemptContext("entry", "query", "attempt-query", 1)
                    result = await assembly.bindings["get_changes"].handler(context, {})
                    self.assertTrue(result["ok"])
                    changes.assert_awaited_once_with(workspace, operations=tuple(
                        operation_id("entry", command.command_id) for command in step.commands), offset=0, text_offset=0)
                    await assembly.bindings["get_changes"].handler(context, {"scope": "workspace"})
                    changes.assert_awaited_with(workspace, operations=None, offset=0, text_offset=0)
                finally:
                    await assembly.scheduler.close()

    async def test_workspace_route_follows_each_command_effect(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "file.txt").write_text("published")
            home = RedPandaHome(root / ".redpanda")
            routes = []
            async def read(self, callback):
                routes.append("read")
                return await callback()
            async def execute(self, identity, callback):
                routes.append("candidate")
                return await callback()
            with (
                patch("redpanda.assistant.assembly.RedPandaHome.default", return_value=home),
                patch("redpanda.assistant.assembly.runtime_data_root", return_value=root / "runtime"),
                patch("redpanda.filesystem.operations.WorkspaceFiles.read", read),
                patch("redpanda.filesystem.operations.WorkspaceFiles.execute", execute),
            ):
                assembly = await build_assistant_assembly(
                    AssistantConfig(model_name="test", compact_threshold_tokens=200_000,
                                    llm=CapturingLlm()),
                    lambda *_: None, MemoryJournal(), session_id="entry",
                    workspace=workspace_record(workspace),
                )
                try:
                    context = AttemptContext("entry", "read", "attempt-read", 1)
                    result = await assembly.bindings["read_file"].handler(context, {"path": "file.txt"})
                    self.assertTrue(result["ok"])
                    self.assertEqual(routes, ["read"])
                    cases = (
                        ({"command": "", "workspace_effect": "read_only"}, "EMPTY_COMMAND", "read"),
                        ({"command": "", "workspace_effect": "may_write"}, "EMPTY_COMMAND", "candidate"),
                        ({"command": ""}, "EMPTY_COMMAND", "candidate"),
                        ({"command": 42, "workspace_effect": "read_only"}, "VALIDATION_ERROR", "read"),
                        ({"command": "", "workspace_effect": "unknown"}, "VALIDATION_ERROR", "candidate"),
                    )
                    for index, (arguments, code, route) in enumerate(cases):
                        with self.subTest(arguments=arguments):
                            context = AttemptContext("entry", f"command-{index}", f"attempt-{index}", 1)
                            result = await assembly.bindings["execute_command"].handler(context, arguments)
                            self.assertEqual(result["code"], code)
                            self.assertEqual(routes.pop(), route)
                    routes.clear()
                    result = await assembly.bindings["execute_command"].handler(context, {
                        "command": "", "file_access": "host",
                    })
                    self.assertEqual(result["code"], "EMPTY_COMMAND")
                    self.assertEqual(routes, [])
                finally:
                    await assembly.scheduler.close()

    async def test_cancelled_model_call_aborts_its_preview(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            previews = []
            llm = BlockingStreamingLlm()
            journal = MemoryJournal()
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
                        llm=llm,
                    ),
                    lambda *_values: None,
                    journal,
                    session_id="session",
                    workspace=workspace_record(workspace),
                    preview_sink=lambda *values: previews.append(values),
                    scheduler_factory=SettlingScheduler,
                )
                try:
                    trigger = await assembly.runtime.receive_user_message(
                        "session",
                        "wait",
                        delivery_id="user-1",
                    )
                    await assembly.scheduler.wake("session")
                    await asyncio.wait_for(llm.started.wait(), timeout=1)
                    await assembly.sessions.cancel_turn("session")
                    await assembly.scheduler.join()

                    self.assertEqual(
                        previews,
                        [
                            ("session", "started", trigger.event_id, None),
                            ("session", "delta", trigger.event_id, "partial"),
                            ("session", "aborted", trigger.event_id, None),
                        ],
                    )
                    events = await journal.snapshot("session")
                    self.assertTrue(
                        any(
                            isinstance(event.payload, DecisionCancelled)
                            for event in events
                        )
                    )
                    self.assertFalse(
                        any(
                            isinstance(event.payload, StepCommitted)
                            for event in events
                        )
                    )
                finally:
                    await assembly.scheduler.close()

    async def test_preview_and_delivery_share_the_trigger_output_id(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            previews = []
            delivered = []
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
                        llm=StreamingLlm(),
                    ),
                    lambda *values: delivered.append(values),
                    MemoryJournal(),
                    session_id="session",
                    workspace=workspace_record(workspace),
                    preview_sink=lambda *values: previews.append(values),
                    scheduler_factory=SettlingScheduler,
                )
                try:
                    trigger = await assembly.runtime.receive_user_message(
                        "session",
                        "hello",
                        delivery_id="user-1",
                    )
                    await assembly.scheduler.wake("session")
                    await assembly.scheduler.join()

                    output_id = trigger.event_id
                    self.assertEqual(
                        previews,
                        [
                            ("session", "started", output_id, None),
                            ("session", "delta", output_id, "hel"),
                            ("session", "delta", output_id, "lo"),
                        ],
                    )
                    self.assertEqual(
                        delivered,
                        [("session", output_id, "hello")],
                    )
                finally:
                    await assembly.scheduler.close()

    async def test_session_endings_are_wired_to_the_subagent_host(self):
        """静止与失败两条线都要落到 SubAgentHost。

        漏接 on_failed，子 Session 撞上模型失败就既不静止也不回收，父会拿着
        一个永远清不空的待回收集合一直等下去。
        """

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            home = RedPandaHome(root / ".redpanda")
            delivered: list[tuple[str, str]] = []
            failed: list[tuple[str, str]] = []
            activity: list[tuple[str, bool]] = []
            with (
                patch(
                    "redpanda.assistant.assembly.RedPandaHome.default",
                    return_value=home,
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
                        llm=CapturingLlm(),
                    ),
                    lambda session_id, _output_id, text: delivered.append(
                        (session_id, text)
                    ),
                    MemoryJournal(),
                    session_id="session",
                    workspace=workspace_record(workspace),
                    session_failed_sink=(
                        lambda session_id, message: failed.append(
                            (session_id, message)
                        )
                    ),
                    subagent_activity_sink=(
                        lambda session_id, active: activity.append(
                            (session_id, active)
                        )
                    ),
                )
                try:
                    scheduler = assembly.scheduler
                    assembly.subagents._parents["parent/sub-1"] = "parent"
                    await scheduler._emit_session_failed(
                        "parent/sub-1", "运行失败：上游 500"
                    )
                    await scheduler._emit_session_failed(
                        "parent", "运行失败：父自己的错误"
                    )

                    assembly.subagents._visible_pending["parent"] = {"parent/sub-1"}
                    assembly.subagents._publish_activity("parent")
                    await asyncio.sleep(0)

                    self.assertEqual(delivered, [])
                    self.assertEqual(failed, [("parent", "运行失败：父自己的错误")])
                    self.assertEqual(activity, [("parent", True)])
                finally:
                    await assembly.scheduler.close()

    async def test_paused_session_does_not_advance(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            paused = False

            async def transport(operation, session_id, arguments):
                if operation == "is_paused":
                    return paused
                if operation == "compact_boundary":
                    return "continue"
                raise AssertionError(operation)

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
                        llm=CapturingLlm(),
                    ),
                    lambda *_values: None,
                    MemoryJournal(),
                    session_id="session",
                    workspace=workspace_record(workspace),
                    scheduler_factory=SettlingScheduler,
                    session_transport=transport,
                )
                try:
                    await assembly.runtime.create_session("session")
                    await assembly.runtime.receive_user_message(
                        "session",
                        "hello",
                        delivery_id="user-1",
                    )
                    self.assertTrue(await assembly.scheduler.before_advance())
                    paused = True
                    self.assertFalse(await assembly.scheduler.before_advance())
                finally:
                    await assembly.scheduler.close()
