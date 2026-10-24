from __future__ import annotations

import asyncio
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from konden.assistant.assembly import build_assistant_assembly
from konden.config import AssistantConfig
from konden.llm.api import LLMCallResult, LLMResponse, LLMUsage
from konden.paths import KondenHome
from konden.runtime import DecisionCancelled, StepCommitted
from tests.fixtures.memory_journal import MemoryJournal
from tests.fixtures.workspaces import workspace_record
from tests.session_scheduler import SettlingScheduler


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
                    "konden.assistant.assembly.KondenHome.default",
                    return_value=KondenHome(root / ".konden"),
                ),
                patch(
                    "konden.assistant.assembly.runtime_data_root",
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
                    "konden.assistant.assembly.KondenHome.default",
                    return_value=KondenHome(root / ".konden"),
                ),
                patch(
                    "konden.assistant.assembly.runtime_data_root",
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
            home = KondenHome(root / ".konden")
            delivered: list[tuple[str, str]] = []
            failed: list[tuple[str, str]] = []
            activity: list[tuple[str, bool]] = []
            with (
                patch(
                    "konden.assistant.assembly.KondenHome.default",
                    return_value=home,
                ),
                patch(
                    "konden.assistant.assembly.runtime_data_root",
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
