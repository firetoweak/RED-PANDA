from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from redpanda.assistant.agents import (
    AgentCreated,
    PrivateMessage,
    Speaker,
    fact_arguments,
)
from redpanda.assistant.artifacts import (
    FileArtifactStore,
)
from redpanda.assistant.compact.core import CompactContext
from redpanda.assistant.context.projection import (
    ModelContextProjector,
    ModelContextSettings,
    externalize_payload,
)
from redpanda.assistant.decision import bind_executor_tools
from redpanda.assistant.delivery import DELIVER_TOOL_NAME, deliver_binding
from redpanda.runtime import (
    AgentRuntime,
    CommandOutcome,
    InvokeTool,
    ModelDecision,
    OutcomeStatus,
    SqliteJournal,
    StateProjector,
    ToolBinding,
)
from redpanda.runtime.dispatcher import AttemptContext, ToolTerminal
from redpanda.runtime.model import MAX_JSON_VALUE_BYTES
from tests.assistant.test_runner import ScriptedDecisionMaker, SequentialIds
from tests.fixtures.artifacts import MemoryArtifactGateway
from tests.fixtures.memory_journal import MemoryJournal
from tests.fixtures.projection import parse_tool_result_meta, project_chat_messages
from tests.session_scheduler import SettlingScheduler, settle_session


class ArtifactBoundaryTest(unittest.TestCase):
    def test_file_store_rejects_artifact_path_traversal(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "artifacts"
            store = FileArtifactStore(root)

            with self.assertRaises(ValueError):
                store.read("../../outside", 0, 10)

            self.assertFalse((Path(directory) / "outside.json").exists())


def _deliver(text: str) -> ModelDecision:
    return ModelDecision(
        content=text,
        command_requests=(
            InvokeTool(
                DELIVER_TOOL_NAME,
                (("output_id", f"output-{text}"), ("text", text)),
            ),
        ),
    )


def _result(data):
    return {"ok": True, "code": "OK", "data": data, "error": None, "hint": None}


class ModelContextProjectorTest(unittest.IsolatedAsyncioTestCase):
    SESSION = "ctx-session"

    async def test_schedule_trigger_times_reach_the_model_in_local_time(self):
        from redpanda.automation.once import TIME_REACHED
        from redpanda.automation.routines import ROUTINE_DUE

        runtime = AgentRuntime(MemoryJournal(), None, {}, SequentialIds())
        await runtime.create_session(self.SESSION)
        stamp = "2026-10-18T23:00:00+00:00"
        for kind, data in ((TIME_REACHED, {"schedule_id": "once", "purpose": "observe"}),
                           (ROUTINE_DUE, {"routine_id": "repeat", "name": "检查", "intent": "observe"})):
            await runtime.receive_domain_fact(self.SESSION, fact_type=kind,
                data={**data, "planned_at": stamp, "fired_at": stamp},
                source="automation", delivery_id=kind, requests_decision=True)
        events = await runtime.snapshot(self.SESSION)
        local = timezone(timedelta(hours=8))

        class LocalDateTime(datetime):
            def astimezone(self, tz=None):
                return super().astimezone(local if tz is None else tz)

        with patch("redpanda.assistant.context.projection.datetime", LocalDateTime, create=True):
            messages = project_chat_messages(events, StateProjector().project_visible(self.SESSION, events))
        shown = json.dumps(messages, ensure_ascii=False)
        self.assertEqual(shown.count("2026-10-19T07:00:00+08:00"), 4)
        self.assertNotIn(stamp, shown)
        # Projection does not rewrite the UTC facts used for durable delivery identity.
        self.assertIn(stamp, str(events))

    async def test_message_during_thinking_follows_the_previous_reply(self):
        for with_tool in (False, True):
            with self.subTest(with_tool=with_tool):
                thinking = asyncio.Event()
                release = asyncio.Event()

                async def first(_frame, thinking=thinking, release=release, with_tool=with_tool):
                    thinking.set()
                    await release.wait()
                    return ModelDecision(
                        content="old reply",
                        command_requests=(InvokeTool("work"),) if with_tool else (),
                    )

                async def work(_context, _arguments):
                    return _result("done")

                model = ScriptedDecisionMaker((first, lambda _: ModelDecision(content="ack")))
                runtime = AgentRuntime(
                    MemoryJournal(), model, {"work": ToolBinding(work)}, SequentialIds()
                )
                await runtime.receive_user_message(self.SESSION, "one", delivery_id="one")
                advancing = asyncio.create_task(runtime.advance(self.SESSION))
                await asyncio.wait_for(thinking.wait(), timeout=1)
                later = await runtime.receive_user_message(self.SESSION, "two", delivery_id="two")
                await runtime.receive_user_message(self.SESSION, "three", delivery_id="three")
                release.set()
                await advancing
                while runtime.dispatcher.active_count:
                    await asyncio.sleep(0)
                await runtime.advance(self.SESSION)
                events = await runtime.snapshot(self.SESSION)

                # 实际决策视图与 Compact 使用的全量归约都应保留相同的对话顺序。
                for state in (
                    model.frames[1].state,
                    StateProjector().project_visible(self.SESSION, events),
                ):
                    prepared = self._projector().prepare(events, state, self.SESSION)
                    shown = [m for m in prepared.messages if m["role"] != "system"]
                    expected = [("user", "one"), ("assistant", "old reply")]
                    if with_tool:
                        expected.append(("tool", None))
                    expected.append(("user", "two"))
                    self.assertEqual([
                        (m["role"], None if m["role"] == "tool" else m["content"])
                        for m in shown[:len(expected)]
                    ], expected)
                    self.assertEqual([
                        seq for seq, message in zip(prepared.source_sequences, prepared.messages)
                        if message["content"] == "two"
                    ], [later.sequence])
                self.assertNotIn("three", str(project_chat_messages(events, model.frames[1].state)))

    async def test_queued_messages_keep_each_reply_with_its_trigger(self):
        model = ScriptedDecisionMaker((
            lambda _: ModelDecision(content="first reply"),
            lambda _: ModelDecision(content="second reply"),
        ))
        runtime = AgentRuntime(MemoryJournal(), model, {}, SequentialIds())
        await runtime.receive_user_message(self.SESSION, "one", delivery_id="one")
        await runtime.receive_user_message(self.SESSION, "two", delivery_id="two")
        await runtime.advance(self.SESSION)
        await runtime.advance(self.SESSION)
        events = await runtime.snapshot(self.SESSION)
        prepared = self._projector().prepare(
            events, StateProjector().project_visible(self.SESSION, events), self.SESSION,
        )
        self.assertEqual([
            (message["role"], message["content"])
            for message in prepared.messages if message["role"] != "system"
        ], [("user", "one"), ("assistant", "first reply"),
            ("user", "two"), ("assistant", "second reply")])

    async def test_error_or_unknown_result_is_not_age_dehydrated(self):
        for ok in (False, None):
            with self.subTest(ok=ok):
                async def failed(_context, _arguments):
                    return {"ok": ok, "code": "MCP_TRANSPORT_ERROR", "error": "offline"}

                events, _ = await self._history(
                    (
                        lambda _frame: ModelDecision(command_requests=(InvokeTool("failed"),)),
                        lambda _frame: _deliver("first-done"),
                        lambda _frame: _deliver("second-done"),
                    ),
                    {"failed": ToolBinding(failed)},
                    ("first", "second"),
                )
                prepared = self._projector().prepare(
                    events, StateProjector().project_visible(self.SESSION, events), self.SESSION,
                )
                self.assertEqual(prepared.age_dehydrated_command_ids, ())
                payload = json.loads(self._tool_messages(prepared.messages)[0]["content"])
                self.assertEqual(set(payload), {"ok", "code", "data", "error", "hint"})
                self.assertIs(payload["ok"], ok)
                self.assertEqual(payload["error"], "offline")

    async def test_execute_time_externalized_failure_keeps_failure_and_preview(self):
        gateway = MemoryArtifactGateway()
        value = {"ok": False, "code": "REMOTE_FAILED", "error": "offline" * 50}
        stub, artifact_id = externalize_payload(
            value, gateway.for_session(self.SESSION), max_chars=80, preview_chars=20,
        )

        async def failed(_context, _arguments):
            return stub

        events, _ = await self._history(
            (
                lambda _frame: ModelDecision(command_requests=(InvokeTool("failed"),)),
                lambda _frame: _deliver("first-done"),
                lambda _frame: _deliver("second-done"),
            ),
            {"failed": ToolBinding(failed)},
            ("first", "second"),
        )
        prepared = self._projector(gateway=gateway).prepare(
            events, StateProjector().project_visible(self.SESSION, events), self.SESSION,
        )
        payload = json.loads(self._tool_messages(prepared.messages)[0]["content"])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "REMOTE_FAILED")
        self.assertEqual(payload["data"]["reference"], artifact_id)
        self.assertTrue(payload["data"]["preview"])
        self.assertEqual(prepared.age_dehydrated_command_ids, ())

    def _projector(self, **overrides) -> ModelContextProjector:
        gateway = overrides.pop("gateway", MemoryArtifactGateway())
        settings = ModelContextSettings(
            size_externalize_chars=overrides.pop("size_externalize_chars", 10_000),
            preview_chars=overrides.pop("preview_chars", 10),
        )
        return ModelContextProjector(
            gateway=gateway,
            settings=settings,
            **overrides,
        )

    async def test_tool_result_follows_its_call_across_an_interleaved_fact(self):
        """工具结果紧跟发起它的 tool_calls，哪怕外部事实先一步落账。

        执行期间到达的用户消息或领域事实，Journal 位置就在 Step 和它的
        Outcome 之间。协议不接受被劈开的 tool_calls。
        """

        started = asyncio.Event()
        gate = asyncio.Event()

        async def work(_context, _arguments):
            started.set()
            await gate.wait()
            return _result("done")

        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker(
                (
                    lambda _frame: ModelDecision(
                        content="working",
                        command_requests=(InvokeTool("work"),),
                    ),
                    lambda _frame: ModelDecision(content="ack"),
                )
            ),
            {"work": ToolBinding(work)},
            SequentialIds(),
        )
        await runtime.receive_user_message(self.SESSION, "go", delivery_id="ask-1")
        await runtime.advance(self.SESSION)
        await started.wait()
        await runtime.receive_user_message(self.SESSION, "wait", delivery_id="ask-2")
        gate.set()
        while runtime.dispatcher.active_count:
            await asyncio.sleep(0)
        await runtime.advance(self.SESSION)

        events = await runtime._journal.snapshot(self.SESSION)
        messages = project_chat_messages(
            events,
            StateProjector().project_visible(self.SESSION, events),
            "sys",
        )
        call = next(
            index
            for index, message in enumerate(messages)
            if message.get("tool_calls")
        )
        self.assertEqual(messages[call + 1]["role"], "tool")
        self.assertEqual(
            messages[call + 1]["tool_call_id"],
            messages[call]["tool_calls"][0]["id"],
        )

    async def test_unauthorized_command_still_gets_a_tool_message(self):
        """未终局的命令也要有工具协议表示，这一帧才配得平。

        用户没有授权就换了话头，下一帧的 tool_calls 至今没有结果。省掉它
        模型会忘记自己发起过；伪造成功或失败都是假话。
        """

        async def secret(_context, _arguments):
            raise AssertionError("unauthorized command must not run")

        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker(
                (
                    lambda _frame: ModelDecision(
                        content="need approval",
                        command_requests=(InvokeTool("secret"),),
                    ),
                    lambda _frame: ModelDecision(content="dropped it"),
                )
            ),
            {"secret": ToolBinding(secret, requires_authorization=True)},
            SequentialIds(),
        )
        await runtime.receive_user_message(self.SESSION, "go", delivery_id="ask-1")
        await runtime.advance(self.SESSION)
        await runtime.receive_user_message(
            self.SESSION, "never mind", delivery_id="ask-2"
        )
        await runtime.advance(self.SESSION)

        events = await runtime._journal.snapshot(self.SESSION)
        messages = project_chat_messages(
            events,
            StateProjector().project_visible(self.SESSION, events),
            "sys",
        )
        call = next(
            index
            for index, message in enumerate(messages)
            if message.get("tool_calls")
        )
        answer = messages[call + 1]
        self.assertEqual(answer["role"], "tool")
        self.assertEqual(
            answer["tool_call_id"], messages[call]["tool_calls"][0]["id"]
        )
        payload = json.loads(answer["content"])
        self.assertIsNone(payload["ok"])
        self.assertEqual(payload["code"], "AWAITING_AUTHORIZATION")
        self.assertIn("尚未执行", payload["hint"])

    async def test_in_flight_command_is_not_reported_as_awaiting_authorization(self):
        """起过 attempt 的命令和等授权的命令必须分开。

        派发会清掉 dispatch_eligible_by_event_id，所以这个字段单独不足以
        判断「在等授权」，得先看 phase。
        """

        started = asyncio.Event()
        gate = asyncio.Event()

        async def work(_context, _arguments):
            started.set()
            await gate.wait()
            return _result("done")

        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker(
                (
                    lambda _frame: ModelDecision(
                        content="working",
                        command_requests=(InvokeTool("work"),),
                    ),
                )
            ),
            {"work": ToolBinding(work)},
            SequentialIds(),
        )
        await runtime.receive_user_message(self.SESSION, "go", delivery_id="ask-1")
        await runtime.advance(self.SESSION)
        await started.wait()

        events = await runtime._journal.snapshot(self.SESSION)
        messages = project_chat_messages(
            events,
            StateProjector().project_visible(self.SESSION, events),
            "sys",
        )
        payload = json.loads(self._tool_messages(messages)[0]["content"])
        self.assertEqual(payload["code"], "NO_RESULT_YET")
        self.assertIn("原因未知", payload["error"])
        self.assertIn("可能仍在执行", payload["error"])
        self.assertNotIn("用户停止", payload["error"])

        gate.set()
        while runtime.dispatcher.active_count:
            await asyncio.sleep(0)

    async def test_recovery_preserves_completed_result_and_guides_unfinished_call(self):
        async def completed(_context, _arguments):
            return _result("done")

        async def unfinished(_context, _arguments):
            raise RuntimeError("worker exited before returning a result")

        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite"
            runtime = AgentRuntime(
                SqliteJournal(path),
                ScriptedDecisionMaker((
                    lambda _frame: ModelDecision(command_requests=(InvokeTool("completed"),)),
                    lambda _frame: ModelDecision(command_requests=(InvokeTool("unfinished"),)),
                )),
                {"completed": ToolBinding(completed), "unfinished": ToolBinding(unfinished)},
                SequentialIds(),
            )
            scheduler = SettlingScheduler(runtime, self.SESSION)
            try:
                await runtime.receive_user_message(self.SESSION, "go", delivery_id="start")
                await scheduler.wake(self.SESSION)
                with self.assertRaisesRegex(RuntimeError, "worker exited"):
                    await scheduler.join()
            finally:
                await scheduler.close()

            events = await SqliteJournal(path).snapshot(self.SESSION)
            prepared = self._projector().prepare(
                events, StateProjector().project_visible(self.SESSION, events), self.SESSION,
            )
            done, unknown = [
                json.loads(message["content"])
                for message in self._tool_messages(prepared.messages)
            ]
            self.assertEqual(done, _result("done"))
            self.assertIsNone(unknown["ok"])
            self.assertEqual(unknown["code"], "NO_RESULT_YET")
            self.assertIn("原因未知", unknown["error"])
            self.assertIn("已部分执行", unknown["error"])
            self.assertIn("先检查相关文件", unknown["hint"])
            self.assertNotIn("用户停止", unknown["error"])

    async def _history(self, scripts, tools, users: tuple[str, ...]):
        delivered: list[str] = []
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker(scripts),
            {**tools, **deliver_binding(lambda _session_id, _output_id, text: delivered.append(text))},
            SequentialIds(),
        )
        for index, text in enumerate(users, start=1):
            await runtime.receive_user_message(
                self.SESSION,
                text,
                delivery_id=f"ask-{index}",
            )
            await settle_session(runtime, self.SESSION)
        events = await runtime._journal.snapshot(self.SESSION)
        return events, delivered

    def _tool_messages(self, messages):
        return [message for message in messages if message["role"] == "tool"]

    async def test_latest_user_turn_is_not_age_dehydrated(self):
        async def ping(_context, _arguments):
            return _result("fresh-result")

        events, _delivered = await self._history(
            (
                lambda _frame: ModelDecision(
                    content="checking",
                    command_requests=(InvokeTool("ping"),),
                ),
                lambda _frame: _deliver("done"),
            ),
            {"ping": ToolBinding(ping)},
            ("go",),
        )
        prepared = self._projector().prepare(
            events,
            StateProjector().project_visible(self.SESSION, events),
            self.SESSION,
            "sys",
        )
        tool = self._tool_messages(prepared.messages)[0]
        self.assertIn("fresh-result", tool["content"])
        self.assertEqual(prepared.age_dehydrated_command_ids, ())

    async def test_a_colleague_message_opens_a_new_protection_window(self):
        async def ping(_context, _arguments):
            return _result("old-result")

        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(content="checking", command_requests=(InvokeTool("ping"),)),
                lambda _frame: _deliver("done"),
                lambda _frame: _deliver("seen"),
            )),
            {"ping": ToolBinding(ping), **deliver_binding(lambda *_values: None)},
            SequentialIds(),
        )
        await runtime.receive_user_message(self.SESSION, "go", delivery_id="ask-1")
        await settle_session(runtime, self.SESSION)
        message = PrivateMessage(
            message_id="private-1", sender=Speaker(kind="agent", name="Ada"), recipient="Lin",
            thread_id="thread-1", hop=0, reply_to=None, text="看一下", origin="origin-1",
        )
        await runtime.receive_domain_fact(self.SESSION, **fact_arguments(message, delivery_id="private-1"))
        await settle_session(runtime, self.SESSION)
        events = await runtime._journal.snapshot(self.SESSION)
        prepared = self._projector().prepare(
            events,
            StateProjector().project_visible(self.SESSION, events),
            self.SESSION,
            "sys",
        )
        self.assertNotIn("old-result", self._tool_messages(prepared.messages)[0]["content"])
        self.assertEqual(len(prepared.age_dehydrated_command_ids), 1)

    async def test_failed_result_before_latest_user_stays(self):
        async def boom(_context, _arguments):
            return ToolTerminal(
                CommandOutcome(
                    OutcomeStatus.FAILED,
                    error_type="Boom",
                    error_message="failed-on-purpose",
                ),
            )

        events, _delivered = await self._history(
            (
                lambda _frame: ModelDecision(
                    content="checking",
                    command_requests=(InvokeTool("boom"),),
                ),
                lambda _frame: _deliver("first-done"),
                lambda _frame: _deliver("second-done"),
            ),
            {"boom": ToolBinding(boom)},
            ("first", "second"),
        )
        prepared = self._projector().prepare(
            events,
            StateProjector().project_visible(self.SESSION, events),
            self.SESSION,
            "sys",
        )
        tool = self._tool_messages(prepared.messages)[0]
        self.assertIn("failed-on-purpose", tool["content"])
        self.assertEqual(prepared.age_dehydrated_command_ids, ())

    async def test_control_results_and_agent_creation_open_new_protection_windows(self):
        from redpanda.assistant.control import (
            CONTROL_CONCLUDED,
            CONTROL_FAILED,
            CONTROL_PREPARATION_FAILED,
            CONTROL_REJECTED,
            CONTROL_SUCCEEDED,
        )
        facts = [{"fact_type": kind, "data": {
            "request_id": "request", "action": "test", "message": "done", "data": {},
        }, "source": "assistant.control", "delivery_id": "control", "requests_decision": True}
            for kind in (CONTROL_CONCLUDED, CONTROL_REJECTED, CONTROL_SUCCEEDED, CONTROL_FAILED,
                         CONTROL_PREPARATION_FAILED)]
        facts.append(fact_arguments(AgentCreated(name="Ada", workspace_id="workspace-test",
                                                worktree=str(Path.cwd()), branch="ada"), delivery_id="created"))
        for fact in facts:
            with self.subTest(fact=fact["fact_type"]):
                async def ping(_context, _arguments):
                    return _result("7 bytes")

                runtime = AgentRuntime(MemoryJournal(), ScriptedDecisionMaker((
                    lambda _: ModelDecision(command_requests=(InvokeTool("ping"),)),
                    lambda _: _deliver("used"),
                    lambda _: _deliver("seen"),
                )), {"ping": ToolBinding(ping), **deliver_binding(lambda *_: None)})
                try:
                    await runtime.receive_user_message(self.SESSION, "go", delivery_id="go")
                    await settle_session(runtime, self.SESSION)
                    await runtime.receive_domain_fact(self.SESSION, **fact)
                    await runtime.advance(self.SESSION)
                    while runtime.dispatcher.active_count:
                        await asyncio.sleep(0)
                    events = await runtime.snapshot(self.SESSION)
                    prepared = self._projector().prepare(
                        events, StateProjector().project_visible(self.SESSION, events), self.SESSION,
                    )
                    self.assertEqual(len(prepared.age_dehydrated_command_ids), 1)
                    tool = self._tool_messages(prepared.messages)[0]
                    self.assertIn("引用不表示原结果很大或失败", tool["content"])
                    self.assertNotIn("7 bytes", tool["content"])
                finally:
                    await runtime.dispatcher.close()

    async def test_oversized_result_in_protection_window_is_stubbed(self):
        blob = "N" * 200

        async def ping(_context, _arguments):
            return _result(blob)

        gateway = MemoryArtifactGateway()
        events, _delivered = await self._history(
            (
                lambda _frame: ModelDecision(
                    content="checking",
                    command_requests=(InvokeTool("ping"),),
                ),
                lambda _frame: _deliver("done"),
            ),
            {"ping": ToolBinding(ping)},
            ("go",),
        )
        prepared = self._projector(
            gateway=gateway,
            size_externalize_chars=80,
        ).prepare(
            events,
            StateProjector().project_visible(self.SESSION, events),
            self.SESSION,
            "sys",
        )
        tool = self._tool_messages(prepared.messages)[0]
        _, artifact_id = parse_tool_result_meta(tool["content"])
        self.assertIsNotNone(artifact_id)
        self.assertNotIn(blob, tool["content"])
        self.assertIn(
            tool["tool_call_id"],
            prepared.size_externalized_command_ids,
        )
        chunk = gateway.for_session(self.SESSION).read(artifact_id, 0, 3000)
        self.assertIn(blob, chunk.content)

    async def test_oversized_failure_keeps_ok_and_code_in_externalized_projection(self):
        failure_body = "remote-failure-" + "X" * 200

        async def boom(_context, _arguments):
            return ToolTerminal(
                CommandOutcome(
                    OutcomeStatus.FAILED,
                    error_type="RemoteError",
                    error_message=failure_body,
                ),
            )

        gateway = MemoryArtifactGateway()
        events, _delivered = await self._history(
            (
                lambda _frame: ModelDecision(
                    content="checking",
                    command_requests=(InvokeTool("boom"),),
                ),
                lambda _frame: _deliver("done"),
            ),
            {"boom": ToolBinding(boom)},
            ("go",),
        )

        prepared = self._projector(
            gateway=gateway,
            size_externalize_chars=80,
        ).prepare(
            events,
            StateProjector().project_visible(self.SESSION, events),
            self.SESSION,
            "sys",
        )

        tool = self._tool_messages(prepared.messages)[0]
        payload = json.loads(tool["content"])
        self.assertEqual(payload["ok"], False)
        self.assertEqual(payload["code"], "RemoteError")
        self.assertNotIn(failure_body, tool["content"])
        artifact_id = payload["data"]["reference"]
        chunk = gateway.for_session(self.SESSION).read(artifact_id, 0, 3000)
        full_outcome = json.loads(chunk.content)
        self.assertEqual(full_outcome["ok"], False)
        self.assertEqual(full_outcome["error"], failure_body)

    async def test_old_oversized_success_drops_preview_without_new_artifact(self):
        blob = "old-large-result-" + "Y" * 200

        async def ping(_context, _arguments):
            return _result(blob)

        gateway = MemoryArtifactGateway()
        events, _delivered = await self._history(
            (
                lambda _frame: ModelDecision(
                    content="checking",
                    command_requests=(InvokeTool("ping"),),
                ),
                lambda _frame: _deliver("first-done"),
                lambda _frame: _deliver("second-done"),
            ),
            {"ping": ToolBinding(ping)},
            ("first", "second"),
        )

        prepared = self._projector(
            gateway=gateway,
            size_externalize_chars=80,
        ).prepare(
            events,
            StateProjector().project_visible(self.SESSION, events),
            self.SESSION,
            "sys",
        )

        tool = self._tool_messages(prepared.messages)[0]
        payload = json.loads(tool["content"])
        artifact_id = payload["data"]["reference"]
        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["data"]["preview"], "")
        self.assertIn(
            tool["tool_call_id"],
            prepared.size_externalized_command_ids,
        )
        self.assertIn(
            tool["tool_call_id"],
            prepared.age_dehydrated_command_ids,
        )
        store = gateway.for_session(self.SESSION)
        self.assertEqual(tuple(store.contents), (artifact_id,))
        full_outcome = json.loads(store.read(artifact_id, 0, 3000).content)
        self.assertEqual(full_outcome["ok"], True)
        self.assertEqual(full_outcome["data"], blob)

    async def test_large_user_input_is_not_blocked_by_local_token_budget(self):
        runtime = AgentRuntime(MemoryJournal(), None, {})
        content = "large input " * 20000
        await runtime.receive_user_message(self.SESSION, content, delivery_id="large")
        events = await runtime.snapshot(self.SESSION)
        prepared = self._projector().prepare(
            events, StateProjector().project_visible(self.SESSION, events), self.SESSION,
        )
        self.assertEqual(prepared.messages[-1]["content"], content)

    async def test_execute_time_externalized_result_uses_canonical_projection(self):
        gateway = MemoryArtifactGateway()
        settings = ModelContextSettings(
            size_externalize_chars=40,
            preview_chars=8,
        )

        class _Runner:
            def names(self):
                return ("blob",)

            def requires_authorization(self, _name):
                return False

            async def execute(self, _name, _arguments):
                return {"ok": True, "code": "OK", "data": "Z" * 80}

        events, _delivered = await self._history(
            (
                lambda _frame: ModelDecision(
                    content="checking",
                    command_requests=(InvokeTool("blob"),),
                ),
                lambda _frame: _deliver("done"),
            ),
            bind_executor_tools(_Runner(), gateway, settings),
            ("go",),
        )
        prepared = ModelContextProjector(
            gateway=gateway,
            settings=settings,
        ).prepare(
            events,
            StateProjector().project_visible(self.SESSION, events),
            self.SESSION,
            "sys",
        )

        tool = self._tool_messages(prepared.messages)[0]
        payload = json.loads(tool["content"])
        self.assertEqual(payload["ok"], True)
        self.assertIn("reference", payload["data"])

    async def test_read_content_pages_are_session_scoped(self):
        gateway = MemoryArtifactGateway()
        artifact = gateway.for_session("s1").save("abcdef")
        reader = CompactContext("s1", (), ModelContextProjector(gateway=gateway), None)
        first = await reader.read_content(
            AttemptContext("s1", "cmd-1", "att-1", 1),
            {"reference": artifact.artifact_id, "offset": 0, "limit": 3},
        )
        self.assertEqual(first["data"]["fragments"][0]["content"], "abc")
        self.assertEqual(first["data"]["next_offset"], 3)
        other = CompactContext("s2", (), ModelContextProjector(gateway=gateway), None)
        missing = await other.read_content(
            AttemptContext("s2", "cmd-1", "att-1", 1),
            {"reference": artifact.artifact_id, "offset": 0, "limit": 3},
        )
        self.assertEqual(missing["code"], "CONTENT_NOT_FOUND")

    def test_externalize_payload_above_threshold_saves_tool_result_artifact(self):
        gateway = MemoryArtifactGateway()
        blob = "oversized-tool-result-" + "Y" * 80
        stub, artifact_id = externalize_payload(
            _result(blob),
            gateway.for_session("s1"),
            max_chars=40,
            preview_chars=12,
        )
        self.assertIsNotNone(artifact_id)
        self.assertEqual(stub["data"]["externalized"], True)
        self.assertEqual(stub["data"]["reference"], artifact_id)
        self.assertTrue(stub["data"]["preview"].startswith('{"ok":'))
        journaled = CommandOutcome(OutcomeStatus.SUCCEEDED, value=stub)
        self.assertEqual(journaled.value["data"]["reference"], artifact_id)
        stored = json.loads(gateway.for_session("s1").read(artifact_id, 0, 4000).content)
        self.assertEqual(stored["ok"], True)
        self.assertEqual(stored["data"], blob)
        self.assertIsNone(stored["error"])
        self.assertIsNone(stored["hint"])

    def test_externalize_payload_larger_than_runtime_freeze_budget(self):
        gateway = MemoryArtifactGateway()
        blob = "x" * (MAX_JSON_VALUE_BYTES + 1)
        stub, artifact_id = externalize_payload(
            _result({"tree": blob}),
            gateway.for_session("s1"),
            max_chars=16_000,
            preview_chars=1_200,
        )
        self.assertIsNotNone(artifact_id)
        CommandOutcome(OutcomeStatus.SUCCEEDED, value=stub)
        store = gateway.for_session("s1")
        head = store.read(artifact_id, 0, 80)
        self.assertGreater(head.total_chars, MAX_JSON_VALUE_BYTES)
        stored = json.loads(store.read(artifact_id, 0, head.total_chars).content)
        self.assertEqual(stored["ok"], True)
        self.assertEqual(stored["data"]["tree"], blob)
