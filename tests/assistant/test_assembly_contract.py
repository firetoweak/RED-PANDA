from __future__ import annotations

import asyncio
import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import pytest

from helperme.assistant.assembly import build_assistant_assembly
from helperme.assistant.context.prompt import DEFAULT_ASSISTANT_PROMPT, environment_prompt
from helperme.assistant.management import LOAD_MANAGEMENT_TOOLS
from helperme.config import AssistantConfig
from helperme.llm.api import LLMCallResult, LLMResponse, LLMUsage, ToolCall
from helperme.paths import HelperMeHome
from helperme.runtime import DecisionCancelled, MemoryJournal, StepCommitted
from tests.fixtures.workspaces import workspace_record
from tests.session_scheduler import (
    SettlingScheduler,
    settle_session,
)


@pytest.fixture(autouse=True)
def version_backend():
    # 本层验证装配；真实 Git 的文件恢复契约在 sandbox process 测试中。
    with patch("helperme.sandbox.versions.WorkspaceVersions.record", return_value="a" * 40):
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
        calls = ()
        if len(self.requests) == 1:
            calls = (
                ToolCall(
                    "load-management",
                    LOAD_MANAGEMENT_TOOLS,
                    '{"domain":"mcp"}',
                ),
            )
        return LLMCallResult(
            LLMResponse(
                content="done",
                calls=calls,
                message_extensions={"reasoning_content": "private-state"},
            ),
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


class AssistantAssemblyContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_assembled_model_request_is_preserved_for_replay(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            home = HelperMeHome(root / ".helperme")
            with (
                patch(
                    "helperme.assistant.assembly.HelperMeHome.default",
                    return_value=home,
                ),
                patch(
                    "helperme.assistant.assembly.runtime_data_root",
                    return_value=root / "runtime",
                ),
            ):
                llm = CapturingLlm()
                journal = MemoryJournal()
                config = AssistantConfig(
                    model_name="test-model",
                    compact_threshold_tokens=200_000,
                    llm=llm,
                )
                session_id = "entry"
                assembly = await build_assistant_assembly(
                    config,
                    lambda _session_id, _output_id, _text: None,
                    journal,
                    session_id=session_id,
                    workspace=workspace_record(workspace),
                )
                try:
                    self.assertIn("restore_workspace", assembly.bindings)
                    self.assertFalse(assembly.bindings["restore_workspace"].requires_authorization)
                    self.assertIsNotNone(assembly.scheduler.record_workspace_versions)
                    decision = assembly.runtime.step_runner._decision_maker
                    self.assertIn("restore_workspace", decision._exclusive_tool_names)
                    await assembly.runtime.receive_user_message(
                        session_id,
                        "检查管理能力",
                        delivery_id="user-1",
                    )
                    state = await assembly.runtime.state(session_id)
                    events = await assembly.runtime.snapshot(session_id)
                    allowed_control = decision._management.control_names(
                        session_id,
                        state,
                    )
                    expected_tools = [
                        *assembly.surface.schemas(session_id, state),
                        *decision._skill_tools.schemas(),
                        *decision._cli_tools.schemas(),
                        *decision._management.schemas(session_id, state),
                        *assembly.control.schemas(
                            session_id, events, allowed_control
                        ),
                        *assembly.subagents.schemas(session_id),
                        *decision._compact.schemas(),
                    ]
                    expected_tools.sort(key=lambda item: item["function"]["name"])
                    environment = environment_prompt(
                        workspace_record(workspace).task_root, full_access=False, own_copy=False,
                    )
                    expected_prompt = f"{DEFAULT_ASSISTANT_PROMPT}\n\n{environment}"

                    first = await assembly.runtime.advance(session_id)
                    await settle_session(
                        assembly.runtime,
                        session_id,
                        control=assembly.control,
                    )

                    request = llm.requests[0]
                    self.assertEqual(request["tools"], expected_tools)
                    self.assertEqual(
                        request["messages"][0],
                        {"role": "system", "content": expected_prompt},
                    )
                    self.assertEqual(
                        [command.effect.name for command in first.step.commands],
                        [LOAD_MANAGEMENT_TOOLS, "deliver"],
                    )
                    event = next(
                        event
                        for event in await journal.snapshot(session_id)
                        if isinstance(event.payload, StepCommitted)
                        and event.payload.step.step_id == first.step.step_id
                    )
                    artifact_id = event.artifact_refs[0]
                    manifest = json.loads(
                        decision._projector.gateway.for_session(session_id)
                        .read(artifact_id, 0, 1_000_000)
                        .content
                    )
                    self.assertEqual(manifest["request"], request)
                    self.assertEqual(
                        event.payload.decision_metadata["model_usage"],
                        {"window": None, **manifest["usage"]},
                    )
                    self.assertEqual(
                        event.payload.decision_metadata["message_extensions"],
                        {"reasoning_content": "private-state"},
                    )
                    replayed = next(
                        message
                        for message in llm.requests[1]["messages"]
                        if message["role"] == "assistant"
                        and message.get("tool_calls")
                    )
                    self.assertEqual(
                        replayed["reasoning_content"], "private-state"
                    )
                    self.assertEqual(
                        replayed["tool_calls"][0]["id"],
                        first.step.commands[0].command_id,
                    )

                    names = assembly.control.names()
                    self.assertTrue(names.isdisjoint(assembly.bindings))
                    self.assertTrue(names.issubset(assembly.surface._reserved))
                finally:
                    await assembly.scheduler.close()


class AssemblyWiringTest(unittest.IsolatedAsyncioTestCase):
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
            home = HelperMeHome(root / ".helperme")
            delivered: list[tuple[str, str]] = []
            failed: list[tuple[str, str]] = []
            activity: list[tuple[str, bool]] = []
            with (
                patch(
                    "helperme.assistant.assembly.HelperMeHome.default",
                    return_value=home,
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
                    self.assertEqual(
                        scheduler._on_quiesced,
                        assembly.subagents.on_quiesced,
                    )
                    self.assertEqual(
                        scheduler._on_failed,
                        assembly.subagents.on_failed,
                    )

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
