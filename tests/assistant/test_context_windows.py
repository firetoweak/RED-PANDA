import json
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image

from helperme.assistant.artifacts import FileArtifactGateway, MemoryArtifactGateway
from helperme.assistant.attachments import AttachmentGateway
from helperme.assistant.compact.core import (
    CompactBoundary,
    CompactContext,
    HANDOFF_PREFIX,
    READ_SCHEMA,
    TASK,
    MODEL_USAGE,
    latest_input_tokens,
    frozen_bundle,
    save_document,
)
from helperme.assistant.context.projection import ModelContextProjector
from helperme.assistant.host.session_store import SessionStore
from helperme.runtime import AgentRuntime, InvokeTool, MemoryJournal, ModelDecision, RecordedDecision, SqliteJournal, StateProjector, ToolBinding
from helperme.assistant.toolsets import ToolSurface
from helperme.assistant.workspaces import SESSION_WORKSPACE_FACT
from tests.assistant.test_toolsets import FakeEchoProvider
from tests.session_scheduler import settle_session


def _png(color: str) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (8, 8), color).save(buffer, format="PNG")
    return buffer.getvalue()


class WindowTest(unittest.IsolatedAsyncioTestCase):
    async def test_history_query_locates_literal_text_within_sequence_range(self):
        runtime = AgentRuntime(MemoryJournal(), None, {})
        await runtime.create_session("b")
        query = r"C:\project\a.*[1].py"
        await runtime.receive_user_message("b", query, delivery_id="before")
        content = "前文" * 300 + query + "\n" + "后文" * 300
        target = await runtime.receive_user_message("b", content, delivery_id="target")
        await runtime.receive_user_message("b", query, delivery_id="after")
        context = CompactContext(
            "b", await runtime.snapshot("b"), ModelContextProjector(gateway=MemoryArtifactGateway()), None,
        )
        context.runtime = runtime
        args = {
            "kind": "view", "reference": "", "offset": 0, "limit": 10,
            "start_sequence": target.sequence, "end_sequence": target.sequence, "query": query,
        }
        result = await context.read(None, args)
        self.assertTrue(result["ok"])
        data = result["data"]
        self.assertEqual(data["total_records"], 1)
        record = data["records"][0]
        self.assertEqual(record["sequence"], target.sequence)
        self.assertIn(query, record["preview"])
        self.assertTrue(record["preview_truncated"])
        event = await context.read(None, {
            "kind": "event", "reference": str(record["sequence"]),
            "offset": 0, "limit": 12000, "upto": data["upto"],
        })
        self.assertIn({"role": "user", "content": content}, json.loads(event["data"]["content"]))
        not_literal = await context.read(None, {**args, "query": r"a.+\[1\]"})
        self.assertEqual(not_literal["data"]["records"], [])
        wrong_case = await context.read(None, {**args, "query": query.lower()})
        self.assertEqual(wrong_case["data"]["records"], [])

    async def test_history_paging_keeps_late_outcomes_out_of_the_original_snapshot(self):
        gateway = MemoryArtifactGateway()
        artifacts = []

        class Decision:
            async def decide(self, frame):
                return ModelDecision(content="query started", command_requests=(InvokeTool("query", ()),))

        async def query(context, arguments):
            artifact = gateway.for_session("b").save("late SQL evidence")
            artifacts.append(artifact.artifact_id)
            return {"ok": True, "code": "QUERY_RESULT", "data": {"artifact_id": artifact.artifact_id}}

        runtime = AgentRuntime(
            MemoryJournal(), Decision(), {"query": ToolBinding(query, decision_on_outcome=False)},
        )
        await runtime.create_session("b")
        await runtime.receive_user_message("b", "original question", delivery_id="first")
        await runtime.advance("b")
        events = await runtime.snapshot("b")
        context = CompactContext("b", events, ModelContextProjector(gateway=gateway), None)
        context.runtime = runtime
        args = {"kind": "view", "reference": "", "offset": 0, "limit": 1}
        first = (await context.read(None, args))["data"]
        self.assertIsNotNone(first["next_offset"])
        await settle_session(runtime, "b")
        await runtime.receive_user_message("b", "new message", delivery_id="new")
        second = (await context.read(None, {
            **args, "upto": first["upto"], "offset": first["next_offset"],
        }))["data"]
        self.assertEqual(second["upto"], first["upto"])
        self.assertEqual(second["total_records"], first["total_records"])
        self.assertIsNone(second["next_offset"])
        step_sequence = second["records"][0]["sequence"]
        event_args = {"kind": "event", "reference": str(step_sequence), "offset": 0, "limit": 12000}
        frozen = await context.read(None, {**event_args, "upto": first["upto"]})
        current = await context.read(None, event_args)
        self.assertNotIn(artifacts[0], frozen["data"]["content"])
        self.assertIn(artifacts[0], current["data"]["content"])
        blocked = await context.read(None, {
            "kind": "artifact", "reference": artifacts[0], "offset": 0, "limit": 100,
            "upto": first["upto"],
        })
        self.assertEqual(blocked["error"], "ARTIFACT_NOT_IN_SOURCE")
        frozen_prefix = frozen["data"]["content"][:40]
        frozen_page = await context.read(None, {**event_args, "limit": 40, "upto": first["upto"]})
        self.assertEqual(frozen_page["data"]["content"], frozen_prefix)
        resumed = await context.read(None, {
            **event_args, "offset": frozen_page["data"]["next_offset"], "upto": first["upto"],
        })
        self.assertEqual(frozen_prefix + resumed["data"]["content"], frozen["data"]["content"])
        continuation = await context.read(None, {**args, "offset": first["next_offset"]})
        self.assertEqual(continuation["code"], "INVALID_ARGUMENT")

    async def test_logical_history_survives_interleaved_compaction_and_nested_forks(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            sid = "b0"
            await store.create(sid, workspace_id="workspace")
            gateway = FileArtifactGateway(store.root)
            attachments = AttachmentGateway(store.root)
            uploaded = attachments.for_session(sid).save_file_stream(BytesIO(b"original log"), "run.log")
            evidence = gateway.for_session(sid).save("original SQL result")
            projector = ModelContextProjector(gateway=gateway, attachments=attachments)

            class Decision:
                async def decide(self, frame):
                    return RecordedDecision(
                        ModelDecision(content="original analysis", command_requests=(InvokeTool("query", ()),)),
                        (), {"message_extensions": {"reasoning_content": "original reasoning"}},
                    )

            async def query(context, arguments):
                return {"ok": True, "code": "QUERY_RESULT", "data": {"artifact_id": evidence.artifact_id}}

            runtime = AgentRuntime(
                SqliteJournal(store.require(sid)), Decision(),
                {"query": ToolBinding(query, decision_on_outcome=False)},
            )
            original = await runtime.receive_user_message(
                sid, "original question", delivery_id="first", artifact_refs=(uploaded.attachment_id,),
            )
            await settle_session(runtime, sid)
            requests = []
            for index in range(3):
                events = await runtime.snapshot(sid)
                context = CompactContext(sid, events, projector, None)
                visible = context.visible(events, StateProjector().project_visible(sid, events))
                prepared = projector.prepare(events, visible, sid, "frozen system", prefix=context.prefix)
                request = save_document(gateway, sid, {"messages": prepared.messages, "tools": [READ_SCHEMA]})
                requests.append(request)
                bundle = save_document(gateway, sid, frozen_bundle(projector, events, sid, context, prepared))
                material = save_document(gateway, sid, {"messages": [{
                    "role": "user", "content": HANDOFF_PREFIX + json.dumps({"source": sid}) + f" handoff {index}",
                }]})
                boundary = CompactBoundary(runtime, None, context, None, None, None)
                await boundary.publish({
                    "handoff": {"artifact": material, "request": request},
                    "window": {
                        "id": str(index), "parent": None if context.window is None else context.window["id"],
                        "upto": events[-1].sequence, "cutover": events[-1].sequence,
                        "context": material, "bundle": bundle,
                    },
                })
                tail = await runtime.receive_user_message(sid, f"tail {index}", delivery_id=f"tail {index}")
                child = f"b{index + 1}"
                await store.fork_after_event(sid, tail.event_id, child)
                await runtime.receive_user_message(sid, f"excluded side {index}", delivery_id="excluded")
                sid = child
                runtime = AgentRuntime(SqliteJournal(store.require(sid)), None, {})

            # Fresh gateways resolve all owners from the inherited logical history.
            attachments = AttachmentGateway(store.root)
            projector = ModelContextProjector(gateway=FileArtifactGateway(store.root), attachments=attachments)
            events = await runtime.snapshot(sid)
            restored = CompactContext(sid, events, projector, None)
            restored.runtime = runtime
            visible = restored.visible(events, StateProjector().project_visible(sid, events))
            self.assertNotIn(original.event_id, visible.visible_event_ids)
            self.assertIn('"source": "b2"', restored.prefix[0]["content"])
            args = {"kind": "view", "reference": "", "offset": 0, "limit": 2}
            pages = []
            while True:
                result = await restored.read(None, args)
                self.assertTrue(result["ok"])
                page = result["data"]
                pages.append(page["records"])
                if page["next_offset"] is None:
                    break
                args["offset"] = page["next_offset"]
                args["upto"] = page["upto"]
            records = [record for page in pages for record in page]
            text = json.dumps(records)
            self.assertGreater(len(pages), 1)
            self.assertNotIn("excluded side", text)
            for index in range(3):
                self.assertIn(f"tail {index}", text)
            self.assertEqual([r["sequence"] for r in records], sorted(r["sequence"] for r in records))
            old_user = next(r for r in records if "original question" in r["preview"])
            self.assertEqual(old_user["sequence"], original.sequence)
            old_step = next(r for r in records if "original analysis" in r["preview"])
            event = await restored.read(None, {**args, "kind": "event", "reference": str(old_step["sequence"]), "offset": 0, "limit": 12000})
            messages = json.loads(event["data"]["content"])
            self.assertEqual(messages[0]["reasoning_content"], "original reasoning")
            artifact_id = json.loads(next(m["content"] for m in messages if m["role"] == "tool"))["data"]["artifact_id"]
            artifact = await restored.read(None, {**args, "kind": "artifact", "reference": artifact_id, "offset": 0, "limit": 12000})
            self.assertEqual(artifact["data"]["content"], "original SQL result")
            request = await restored.read(None, {**args, "kind": "artifact", "reference": requests[0], "offset": 0, "limit": 12000})
            self.assertIn("frozen system", request["data"]["content"])
            material_path = attachments.for_session(sid).files.materials / uploaded.attachment_id.removeprefix("file:") / uploaded.name
            self.assertEqual(material_path.read_bytes(), b"original log")

    async def test_compact_uses_last_committed_usage_in_the_current_window(self):
        gateway = MemoryArtifactGateway()
        projector = ModelContextProjector(gateway=gateway)
        usage = {"window": None, "input_tokens": 199999, "cached_input_tokens": 199999}

        class Decision:
            async def decide(self, frame):
                return RecordedDecision(ModelDecision(content="done"), (), {MODEL_USAGE: usage})

        runtime = AgentRuntime(MemoryJournal(), Decision(), {})
        await runtime.create_session("b")
        transport = AsyncMock(return_value="continue")

        async def check(expected):
            # Reconstruct the context each time: usage must survive Worker restarts.
            events = await runtime.snapshot("b")
            context = CompactContext("b", events, projector, None)
            boundary = CompactBoundary(
                runtime, None, context,
                SimpleNamespace(compact_threshold_tokens=200000), None, transport,
            )
            self.assertTrue(await boundary.before_advance())
            transport.assert_awaited_with("compact_boundary", "b", {"pressure": expected})
            return boundary

        await runtime.receive_user_message("b", "large input " * 20000, delivery_id="one")
        await check(False)  # No response yet; request size never triggers an estimate.
        await runtime.advance("b")
        await check(False)
        usage["input_tokens"] = 200000
        await runtime.receive_user_message("b", "next", delivery_id="two")
        await runtime.advance("b")
        boundary = await check(True)  # Includes cached input and uses >=, not >.
        events = await runtime.snapshot("b")
        material = save_document(gateway, "b", {"messages": [{"role": "user", "content": "handoff"}]})
        await boundary.publish({
            "handoff": {"artifact": material, "request": material},
            "window": {
                "id": "new-window", "parent": None, "upto": events[-1].sequence,
                "cutover": events[-1].sequence, "context": material, "bundle": material,
            },
        })
        await check(False)
        # Even a delayed result from the former window must not trigger again.
        await runtime.receive_user_message("b", "late", delivery_id="three")
        await runtime.advance("b")
        await check(False)
        usage.update(window="new-window", input_tokens=1, cached_input_tokens=0)
        await runtime.receive_user_message("b", "new request", delivery_id="four")
        await runtime.advance("b")
        self.assertEqual(latest_input_tokens(await runtime.snapshot("b")), 1)
        await check(False)  # Previous calls are not added to the current usage.

    async def test_step_without_usage_is_skipped(self):
        usage = {"window": None, "input_tokens": 12, "cached_input_tokens": 0}
        decisions = [
            RecordedDecision(ModelDecision(content="done"), (), {MODEL_USAGE: usage}),
            ModelDecision(content="again"),
        ]

        class Decision:
            async def decide(self, frame):
                return decisions.pop(0)

        runtime = AgentRuntime(MemoryJournal(), Decision(), {})
        await runtime.create_session("b")
        await runtime.receive_user_message("b", "one", delivery_id="one")
        await runtime.advance("b")
        await runtime.receive_user_message("b", "two", delivery_id="two")
        await runtime.advance("b")
        self.assertEqual(latest_input_tokens(await runtime.snapshot("b")), 12)

    async def test_frozen_bundle_keeps_user_image_blocks(self):
        with TemporaryDirectory() as directory:
            attachments = AttachmentGateway(Path(directory))
            store = attachments.for_session("b")
            buffer = BytesIO()
            Image.new("RGB", (8, 8), "red").save(buffer, format="PNG")
            ref = store.save_image(buffer.getvalue(), "image/png")
            projector = ModelContextProjector(
                gateway=MemoryArtifactGateway(),
                attachments=attachments,
            )
            runtime = AgentRuntime(MemoryJournal(), None, {})
            await runtime.create_session("b")
            await runtime.receive_user_message(
                "b",
                "[Image #1] look",
                delivery_id="u",
                artifact_refs=(ref.attachment_id,),
            )
            events = await runtime.snapshot("b")
            context = CompactContext("b", events, projector, None)
            bundle = frozen_bundle(projector, events, "b", context)
            user = next(
                message
                for messages in bundle["raw"].values()
                for message in messages
                if message["role"] == "user"
            )
            self.assertTrue(user["content"][0]["text"].startswith("[Image #1] look"))
            self.assertEqual(user["content"][1]["id"], ref.attachment_id)
            self.assertEqual(user["content"][1]["type"], "image")

    async def test_reader_reads_frozen_prefix_attachments_from_source(self):
        with TemporaryDirectory() as directory:
            attachments = AttachmentGateway(Path(directory))
            source_store = attachments.for_session("b")
            own_store = attachments.for_session("h")
            source_ref = source_store.save_image(_png("red"), "image/png")
            own_ref = own_store.save_image(_png("blue"), "image/png")
            gateway = MemoryArtifactGateway()
            inherited = save_document(
                gateway,
                "b",
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "look"},
                                {
                                    "type": "file",
                                    "id": source_ref.attachment_id,
                                    "mime": "image/png",
                                },
                            ],
                        }
                    ],
                    "tools": [],
                },
            )
            bundle = save_document(
                gateway,
                "b",
                {"records": [], "raw": {}, "artifacts": []},
            )
            projector = ModelContextProjector(
                gateway=gateway,
                attachments=attachments,
            )
            runtime = AgentRuntime(MemoryJournal(), None, {})
            await runtime.create_session("h")
            await runtime.receive_domain_fact(
                "h",
                SESSION_WORKSPACE_FACT,
                {"workspace_id": "workspace"},
                source="workspace",
                delivery_id="binding",
            )
            await runtime.receive_domain_fact(
                "h",
                TASK,
                {
                    "source": "b",
                    "bundle": bundle,
                    "inherited": inherited,
                    "upto": 1,
                    "window": None,
                },
                source="compact",
                delivery_id="task",
            )
            context = CompactContext(
                "h", await runtime.snapshot("h"), projector, None
            )
            self.assertEqual(
                context.read_attachment(source_ref.attachment_id),
                source_store.read(source_ref.attachment_id),
            )
            self.assertFalse(own_store.path(source_ref.attachment_id).is_file())
            self.assertEqual(
                context.read_attachment(own_ref.attachment_id),
                own_store.read(own_ref.attachment_id),
            )

    async def test_read_is_limited_to_frozen_source_even_when_business_log_grows(self):
        gateway = MemoryArtifactGateway()
        projector = ModelContextProjector(gateway=gateway)
        runtime = AgentRuntime(MemoryJournal(), None, {})
        await runtime.create_session("h")
        await runtime.receive_domain_fact(
            "h",
            SESSION_WORKSPACE_FACT,
            {"workspace_id": "workspace"},
            source="workspace",
            delivery_id="binding",
        )
        bundle = save_document(
            gateway,
            "b",
            {
                "records": [],
                "raw": {"1": [{"role": "user", "content": "before P"}]},
                "artifacts": [],
            },
        )
        request = save_document(
            gateway,
            "b",
            {
                "messages": [{"role": "system", "content": "fixed"}],
                "tools": [],
            },
        )
        await runtime.receive_domain_fact(
            "h",
            TASK,
            {
                "source": "b",
                "bundle": bundle,
                "inherited": request,
                "upto": 1,
                "window": None,
            },
            source="compact",
            delivery_id="task",
        )
        context = CompactContext("h", await runtime.snapshot("h"), projector, None)
        context.runtime = runtime
        await runtime.create_session("b")
        await runtime.receive_user_message("b", "before P", delivery_id="before")
        await runtime.receive_user_message("b", "after P", delivery_id="after")
        later_artifact = gateway.for_session("b").save("after P artifact")
        request_read = await context.read(
            None,
            {
                "kind": "artifact",
                "reference": request,
                "offset": 0,
                "limit": 1000,
            },
        )
        self.assertIn("fixed", request_read["data"]["content"])

        args = {
            "kind": "event",
            "reference": "1",
            "offset": 0,
            "limit": 1000,
        }
        self.assertIn("before P", (await context.read(None, args))["data"]["content"])
        self.assertEqual(
            (await context.read(None, {**args, "reference": "2"}))["error"],
            "EVENT_NOT_IN_SOURCE",
        )
        view = await context.read(None, {**args, "kind": "view", "reference": "", "limit": 10})
        self.assertEqual(view["data"]["upto"], 1)
        self.assertEqual(view["data"]["records"], [{
            "sequence": 1, "roles": ["user"], "preview": "user\nbefore P", "preview_truncated": False,
        }])
        scoped_view = await context.read(None, {
            "kind": "view", "reference": "", "offset": 0, "limit": 10,
            "upto": view["data"]["upto"], "start_sequence": 1, "end_sequence": 1, "query": "before",
        })
        self.assertEqual(scoped_view["data"]["records"], [{
            "sequence": 1, "roles": ["user"], "preview": "before P", "preview_truncated": False,
        }])
        beyond_frozen = await context.read(None, {**args, "upto": 2})
        self.assertEqual(beyond_frozen["code"], "HISTORY_POSITION_OUT_OF_RANGE")
        self.assertEqual(
            (await context.read(None, {**args, "kind": "artifact", "reference": later_artifact.artifact_id}))["error"],
            "ARTIFACT_NOT_IN_SOURCE",
        )
        self.assertEqual(
            (await context.read(None, {**args, "source": "other"}))["error"],
            "INVALID_ARGUMENT",
        )

    async def test_windows_preserve_execution_state_and_rebuild_from_journal(self):
        gateway = MemoryArtifactGateway()
        projector = ModelContextProjector(gateway=gateway)
        runtime = AgentRuntime(MemoryJournal(), None, {})
        await runtime.create_session("b")
        await runtime.receive_user_message("b", "original", delivery_id="first")
        surface = ToolSurface(providers=(FakeEchoProvider(),))
        surface.attach(runtime)
        surface.apply_catalog("b", surface.registry_descriptors())
        await surface.load("b", "demo")
        schemas = surface.schemas("b")
        context = CompactContext("b", await runtime.snapshot("b"), projector, None)
        boundary = CompactBoundary(runtime, None, context, None, None, None)
        initial = await runtime.snapshot("b")
        for index in range(2):
            material = save_document(
                gateway,
                "b",
                {"messages": [{"role": "user", "content": f"handoff {index}"}]},
            )
            args = {
                "handoff": {"artifact": material, "request": material},
                "window": {
                    "id": str(index),
                    "parent": None if index == 0 else str(index - 1),
                    "upto": 1,
                    "cutover": len(await runtime.snapshot("b")),
                    "context": material,
                    "bundle": material,
                },
            }
            await boundary.publish(args)
            before = await runtime.snapshot("b")
            await boundary.publish(args)
            self.assertEqual(await runtime.snapshot("b"), before)
            self.assertEqual(surface.schemas("b"), schemas)
        events = await runtime.snapshot("b")
        self.assertEqual(events[: len(initial)], initial)
        restored = CompactContext("b", events, projector, None)
        self.assertEqual(restored.window["id"], "1")
        self.assertEqual(restored.prefix[0]["content"], "handoff 1")
        restored.runtime = runtime
        evidence = await restored.read(
            None,
            {
                "kind": "artifact",
                "reference": material,
                "offset": 0,
                "limit": 1000,
            },
        )
        self.assertIn("handoff 1", evidence["data"]["content"])

        def restored_visible(events):
            whole = StateProjector().project_visible("b", events)
            return restored.visible(events, whole).visible_event_ids

        self.assertEqual(restored_visible(events), ())
        await runtime.receive_user_message("b", "new", delivery_id="new")
        events = await runtime.snapshot("b")
        self.assertEqual(restored_visible(events), (events[-1].event_id,))
        with self.assertRaisesRegex(ValueError, "stale"):
            await boundary.publish(
                {**args, "window": {**args["window"], "id": "stale", "parent": None}}
            )
