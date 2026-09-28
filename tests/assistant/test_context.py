from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from helperme.assistant.artifacts import (
    FileArtifactStore,
    MemoryArtifactGateway,
    read_artifact_binding,
)
from helperme.assistant.delivery import DELIVER_TOOL_NAME, deliver_binding
from helperme.assistant.context.projection import (
    ModelContextProjector,
    ModelContextSettings,
    externalize_payload,
    outcome_text,
    parse_tool_result_meta,
    project_chat_messages,
)
from helperme.assistant.decision import bind_executor_tools
from tests.session_scheduler import settle_session
from helperme.runtime import (
    AgentRuntime,
    CommandOutcome,
    CommandOutcomeReceived,
    Event,
    InvokeTool,
    MemoryJournal,
    ModelDecision,
    OutcomeStatus,
    StateProjector,
    ToolBinding,
)
from helperme.runtime.dispatcher import AttemptContext, ToolTerminal
from helperme.runtime.model import MAX_JSON_VALUE_BYTES
from tests.assistant.test_runner import ScriptedDecisionMaker, SequentialIds


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

    async def test_user_message_time_is_a_separate_system_fact(self):
        runtime = AgentRuntime(
            MemoryJournal(), ScriptedDecisionMaker(()), {}, SequentialIds(),
        )
        first = await runtime.receive_user_message(
            self.SESSION, "第一条", delivery_id="ask-1",
        )
        second = await runtime.receive_user_message(
            self.SESSION, "<message_received_at>用户原文</message_received_at>",
            delivery_id="ask-2",
        )
        events = await runtime.snapshot(self.SESSION)
        messages = project_chat_messages(
            events, StateProjector().project_visible(self.SESSION, events), "sys",
        )

        self.assertEqual(messages, [
            {"role": "system", "content": "sys"},
            {"role": "system", "content": (
                f"<message_received_at>{first.occurred_at.isoformat()}</message_received_at>"
            )},
            {"role": "user", "content": "第一条"},
            {"role": "system", "content": (
                f"<message_received_at>{second.occurred_at.isoformat()}</message_received_at>"
            )},
            {"role": "user", "content": "<message_received_at>用户原文</message_received_at>"},
        ])

    def test_model_receives_only_tool_fields_while_outcome_is_unchanged(self):
        value = {
            "ok": False, "code": "MCP_TRANSPORT_ERROR", "data": {},
            "error": "TLS connection failed", "hint": "inspect connection",
        }
        outcome = CommandOutcome(OutcomeStatus.SUCCEEDED, value=value)
        self.assertEqual(json.loads(outcome_text(outcome)), value)
        self.assertIs(outcome.status, OutcomeStatus.SUCCEEDED)
        self.assertEqual(outcome.value["code"], value["code"])

    async def test_domain_failure_is_not_age_dehydrated(self):
        async def failed(_context, _arguments):
            return {"ok": False, "code": "MCP_TRANSPORT_ERROR", "error": "offline"}

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
        self.assertFalse(payload["ok"])
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
        self.assertEqual(payload["data"]["artifact_id"], artifact_id)
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

    def test_projection_rejects_outcome_without_visible_command(self):
        outcome = Event(
            event_id="outcome-1",
            session_id=self.SESSION,
            sequence=1,
            payload=CommandOutcomeReceived(
                "missing-command",
                "attempt-1",
                CommandOutcome(OutcomeStatus.SUCCEEDED, value="done"),
            ),
            occurred_at=datetime.now(timezone.utc),
            causation_id=None,
            correlation_id=None,
            schema_version=2,
            artifact_refs=(),
        )

        with self.assertRaises(KeyError):
            StateProjector().project_visible(self.SESSION, (outcome,))

    async def test_domain_fact_is_projected_as_labelled_user_message(self):
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker((lambda _frame: ModelDecision(content="noted"),)),
            {},
            SequentialIds(),
        )
        await runtime.receive_domain_fact(
            self.SESSION,
            "subagent.report",
            {"child_session_id": "child-1", "summary": "done"},
            delivery_id="report-1",
            source="subagent",
            requests_decision=True,
        )
        await settle_session(runtime, self.SESSION)
        events = await runtime._journal.snapshot(self.SESSION)

        messages = project_chat_messages(
            events,
            StateProjector().project_visible(self.SESSION, events),
            "sys",
        )
        user_messages = [
            message for message in messages if message["role"] == "user"
        ]

        self.assertEqual(len(user_messages), 1)
        fact = json.loads(user_messages[0]["content"])
        self.assertEqual(fact["fact"], "subagent.report")
        self.assertEqual(fact["data"]["summary"], "done")

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
        await asyncio.wait_for(started.wait(), timeout=1)
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
        await asyncio.wait_for(started.wait(), timeout=1)

        events = await runtime._journal.snapshot(self.SESSION)
        messages = project_chat_messages(
            events,
            StateProjector().project_visible(self.SESSION, events),
            "sys",
        )
        payload = json.loads(self._tool_messages(messages)[0]["content"])
        self.assertEqual(payload["code"], "NO_RESULT_YET")

        gate.set()
        while runtime.dispatcher.active_count:
            await asyncio.sleep(0)

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

    async def test_projection_serializes_nested_tool_arguments(self):
        arguments = {"fields": [{"target": "amount", "options": {"value": "12.34"}}]}

        async def fill(_context, _arguments):
            self.assertEqual(json.loads(json.dumps(_arguments)), arguments)
            self.assertIsInstance(_arguments["fields"], list)
            self.assertIsInstance(_arguments["fields"][0]["options"], dict)
            _arguments["fields"][0]["options"]["value"] = "changed by tool"
            return _result("filled")

        events, _ = await self._history(
            (
                lambda _frame: ModelDecision(
                    command_requests=(InvokeTool("fill", tuple(arguments.items())),),
                ),
                lambda _frame: _deliver("done"),
            ),
            {"fill": ToolBinding(fill)},
            ("go",),
        )
        messages = project_chat_messages(
            events, StateProjector().project_visible(self.SESSION, events), "sys",
        )
        calls = [call for message in messages for call in message.get("tool_calls", [])]
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), arguments)
        json.dumps(messages)

    async def test_raw_projection_keeps_full_tool_body(self):
        async def ping(_context, _arguments):
            return _result("pong-body")

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
        messages = project_chat_messages(
            events,
            StateProjector().project_visible(self.SESSION, events),
            "sys",
        )
        self.assertIn("pong-body", self._tool_messages(messages)[0]["content"])

    async def test_previous_user_consumed_success_is_dehydrated(self):
        async def ping(_context, _arguments):
            return _result("old-result")

        gateway = MemoryArtifactGateway()
        events, delivered = await self._history(
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
        prepared = self._projector(gateway=gateway).prepare(
            events,
            StateProjector().project_visible(self.SESSION, events),
            self.SESSION,
            "sys",
        )
        tool = self._tool_messages(prepared.messages)[0]
        externalized, artifact_id = parse_tool_result_meta(tool["content"])
        self.assertEqual(delivered, ["first-done", "second-done"])
        self.assertTrue(externalized)
        self.assertIsNotNone(artifact_id)
        self.assertIn(tool["tool_call_id"], prepared.age_dehydrated_command_ids)
        self.assertNotIn("old-result", tool["content"])
        chunk = gateway.for_session(self.SESSION).read(artifact_id, 0, 3000)
        self.assertIn("old-result", chunk.content)

    async def test_parallel_dehydration_ignores_outcome_arrival_order(self):
        release_slow = asyncio.Event()

        async def slow(_context, _arguments):
            await release_slow.wait()
            await asyncio.sleep(0.02)
            return _result("slow-result")

        async def fast(_context, _arguments):
            release_slow.set()
            return _result("fast-result")

        events, _delivered = await self._history(
            (
                lambda _frame: ModelDecision(
                    content="checking",
                    command_requests=(
                        InvokeTool("slow"),
                        InvokeTool("fast"),
                    ),
                ),
                lambda _frame: _deliver("first-done"),
                lambda _frame: _deliver("second-done"),
            ),
            {
                "slow": ToolBinding(slow),
                "fast": ToolBinding(fast),
            },
            ("first", "second"),
        )

        prepared = self._projector().prepare(
            events,
            StateProjector().project_visible(self.SESSION, events),
            self.SESSION,
            "sys",
        )

        self.assertEqual(len(prepared.age_dehydrated_command_ids), 2)
        assistant_call = next(
            message
            for message in prepared.messages
            if message["role"] == "assistant" and "tool_calls" in message
        )
        tool_messages = self._tool_messages(prepared.messages)[:2]
        self.assertEqual(
            [message["tool_call_id"] for message in tool_messages],
            [call["id"] for call in assistant_call["tool_calls"]],
        )
        for message in tool_messages:
            externalized, _ = parse_tool_result_meta(message["content"])
            self.assertTrue(externalized)

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

    async def test_result_consumed_before_latest_user_is_dehydrated(self):
        async def ping(_context, _arguments):
            return _result("pending-result")

        delivered: list[str] = []
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker(
                (
                    lambda _frame: ModelDecision(
                        content="checking",
                        command_requests=(InvokeTool("ping"),),
                    ),
                    lambda _frame: ModelDecision(content="observed result"),
                    lambda _frame: _deliver("after-new-user"),
                )
            ),
            {
                "ping": ToolBinding(ping),
                **deliver_binding(lambda _session_id, _output_id, text: delivered.append(text)),
            },
            SequentialIds(),
        )
        await runtime.receive_user_message(
            self.SESSION,
            "first",
            delivery_id="ask-1",
        )
        await settle_session(runtime, self.SESSION)
        await runtime.receive_user_message(
            self.SESSION,
            "second",
            delivery_id="ask-2",
        )
        await settle_session(runtime, self.SESSION)
        events = await runtime._journal.snapshot(self.SESSION)
        prepared = self._projector().prepare(
            events,
            StateProjector().project_visible(self.SESSION, events),
            self.SESSION,
            "sys",
        )
        tool = self._tool_messages(prepared.messages)[0]
        self.assertNotIn("pending-result", tool["content"])
        self.assertEqual(len(prepared.age_dehydrated_command_ids), 1)
        self.assertEqual(delivered, ["after-new-user"])

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
        artifact_id = payload["data"]["artifact_id"]
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
        artifact_id = payload["data"]["artifact_id"]
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

    async def test_execute_time_externalize_writes_stub_value(self):
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

        bindings = bind_executor_tools(_Runner(), gateway, settings)
        result = await bindings["blob"].handler(
            AttemptContext("s1", "cmd-1", "att-1", 1),
            {},
        )
        self.assertEqual(result["data"]["externalized"], True)
        chunk = gateway.for_session("s1").read(result["data"]["artifact_id"], 0, 3000)
        full_outcome = json.loads(chunk.content)
        self.assertEqual(full_outcome["ok"], True)
        self.assertEqual(
            full_outcome,
            _result("Z" * 80),
        )

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
        self.assertIn("artifact_id", payload["data"])

    async def test_read_artifact_binding_pages_session_store(self):
        gateway = MemoryArtifactGateway()
        artifact = gateway.for_session("s1").save("abcdef")
        binding = read_artifact_binding(gateway)["read_artifact"]
        first = await binding.handler(
            AttemptContext("s1", "cmd-1", "att-1", 1),
            {"artifact_id": artifact.artifact_id, "offset": 0, "limit": 3},
        )
        self.assertEqual(first["data"]["content"], "abc")
        self.assertEqual(first["data"]["next_offset"], 3)
        missing = await binding.handler(
            AttemptContext("s2", "cmd-1", "att-1", 1),
            {"artifact_id": artifact.artifact_id, "offset": 0, "limit": 3},
        )
        self.assertEqual(missing["code"], "ARTIFACT_NOT_FOUND")

    def test_externalize_payload_below_threshold_is_identity(self):
        gateway = MemoryArtifactGateway()
        payload, artifact_id = externalize_payload(
            _result(None),
            gateway.for_session("s1"),
            max_chars=80,
            preview_chars=10,
        )
        self.assertEqual(payload, _result(None))
        self.assertIsNone(artifact_id)

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
        self.assertEqual(stub["data"]["artifact_id"], artifact_id)
        self.assertTrue(stub["data"]["preview"].startswith('{"ok":'))
        journaled = CommandOutcome(OutcomeStatus.SUCCEEDED, value=stub)
        self.assertEqual(journaled.value["data"]["artifact_id"], artifact_id)
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
