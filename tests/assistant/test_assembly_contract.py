from __future__ import annotations

import asyncio
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from konden.assistant.assembly import build_assistant_assembly
from konden.assistant.subagent.subagent import (
    CLOSED_FACT, RETURN_FACT, DelegateIntent, child_session_id, task_fact_arguments,
)
from konden.config import AssistantConfig
from konden.llm.api import LLMCallResult, LLMResponse, LLMUsage
from konden.paths import KondenHome
from konden.runtime import AgentRuntime, DecisionCancelled, StepCommitted
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

    async def test_child_failure_records_return_and_only_reports_parent_failure(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            journal = MemoryJournal()
            failed = []
            child = child_session_id("parent", "delegate-1")
            await AgentRuntime(journal, None, {}).receive_domain_fact(child, **task_fact_arguments(
                DelegateIntent("delegate-1", "parent", child, "核对结果"),
            ))

            async def transport(operation, session_id, arguments):
                self.assertEqual(operation, "fact")
                await assembly.runtime.receive_domain_fact(session_id, **arguments)

            with (
                patch("konden.assistant.assembly.KondenHome.default",
                      return_value=KondenHome(root / ".konden")),
                patch("konden.assistant.assembly.runtime_data_root",
                      return_value=root / "runtime"),
            ):
                assembly = await build_assistant_assembly(
                    AssistantConfig(model_name="test-model", compact_threshold_tokens=200_000,
                                    llm=CapturingLlm()),
                    lambda *_values: None,
                    journal,
                    session_id=child,
                    workspace=workspace_record(workspace),
                    session_failed_sink=lambda session_id, message: failed.append((session_id, message)),
                    session_transport=transport,
                )
                try:
                    # Use the scheduler's two failure notifications, as a failed
                    # model advance does: the child must return to its parent.
                    await assembly.scheduler._emit_session_failed(child, "运行失败：上游 500")
                    await assembly.scheduler._failed(child, "上游 500")
                    await assembly.scheduler._emit_session_failed("parent", "运行失败：父自己的错误")
                    returned = [event.payload.data for event in await journal.snapshot(child)
                                if getattr(event.payload, "fact_type", None) == RETURN_FACT]
                    closed = [event.payload.data for event in await journal.snapshot("parent")
                              if getattr(event.payload, "fact_type", None) == CLOSED_FACT]
                    self.assertEqual(returned, [{
                        "child_session_id": child, "failure": "上游 500", "reason": None,
                    }])
                    self.assertEqual(closed, returned)
                    self.assertEqual(failed, [("parent", "运行失败：父自己的错误")])
                finally:
                    await assembly.scheduler.close()
