from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from redpanda.assistant.attachments import (
    AttachmentGateway,
    AttachmentRejected,
    MAX_SOURCE_BYTES,
)
from redpanda.assistant.builtin_tools import build_builtin_tools
from redpanda.assistant.context.projection import project_chat_messages
from redpanda.assistant.conversations import project_conversation
from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.sessions import SessionView
from redpanda.runtime import AgentRuntime, MemoryJournal, SqliteJournal, StateProjector
from redpanda.sandbox.registry import WorkspaceRecord
from tests.assistant.test_runner import ScriptedDecisionMaker, SequentialIds


def _save(store, data: bytes, name: str):
    return store.save_file_stream(BytesIO(data), name)


class FileAttachmentsTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_committed_files_enter_materials_and_conversation(self):
        with TemporaryDirectory() as directory:
            store = AttachmentGateway(Path(directory)).for_session("session")
            sent = _save(store, b"original", "中文报告.docx")
            staged = _save(store, b"draft", "中文报告.docx")
            self.assertNotEqual(sent.attachment_id, staged.attachment_id)
            self.assertFalse(store.files.materials.exists())
            runtime = AgentRuntime(MemoryJournal(), ScriptedDecisionMaker(()), {}, SequentialIds())
            await runtime.receive_user_message(
                "session", "分析 [File #1]", delivery_id="user", artifact_refs=(sent.attachment_id,),
            )
            events = await runtime.snapshot("session")
            state = StateProjector().project_visible("session", events)
            messages = project_chat_messages(events, state, "sys", attachments=store)
            self.assertIn(sent.attachment_id, messages[2]["content"])
            self.assertNotIn(staged.attachment_id, json.dumps(messages))
            self.assertEqual(len(tuple(store.files.materials.iterdir())), 1)
            self.assertEqual(store.files.materialize(sent.attachment_id).read_bytes(), b"original")
            view = project_conversation("session", events, (), session=SessionView("waiting", (), (), False), attachments=store)
            self.assertEqual(view.items[0].images, ())
            self.assertEqual(view.items[0].files, (sent,))

    async def test_file_tools_read_materials_but_write_only_workspace_copy(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "workspace"
            task.mkdir()
            store = AttachmentGateway(root / "sessions").for_session("session")
            ref = _save(store, b"original", "notes.txt")
            material = store.files.materialize(ref.attachment_id)
            runner = await build_builtin_tools(
                WorkspaceRecord("workspace-test", "test", task, True, "2026-09-27"),
                materials_root=store.files.materials,
            )
            read = await runner.execute("read_file", {"path": str(material)})
            self.assertTrue(read["ok"])
            write = await runner.execute("write_file", {"path": str(material), "content": "changed"})
            self.assertEqual(write["code"], "ENVIRONMENT_PERMISSION_DENIED")
            copied = task / ref.name
            copied.write_bytes(material.read_bytes())
            write = await runner.execute("write_file", {"path": str(copied), "content": "changed", "overwrite": True})
            self.assertTrue(write["ok"])
            self.assertEqual(copied.read_text(), "changed")
            self.assertEqual(store.read(ref.attachment_id), b"original")
            self.assertEqual(material.read_bytes(), b"original")

    async def test_fork_rebuilds_its_own_materials_from_inherited_originals(self):
        with TemporaryDirectory() as directory:
            sessions = SessionStore(Path(directory))
            await sessions.create("source", workspace_id="workspace-test")
            gateway = AttachmentGateway(sessions.root)
            source = gateway.for_session("source")
            ref = _save(source, b"original", "report.pdf")
            runtime = AgentRuntime(SqliteJournal(sessions.require("source")), ScriptedDecisionMaker(()), {}, SequentialIds())
            event = await runtime.receive_user_message("source", "[File #1]", delivery_id="input", artifact_refs=(ref.attachment_id,))
            source.files.materialize(ref.attachment_id)
            self.assertTrue(source.files.materials.exists())
            await sessions.fork_after_event("source", event.event_id, "branch")
            branch = gateway.for_session("branch")
            self.assertFalse(branch.files.materials.exists())
            events = await SqliteJournal(sessions.require("branch")).snapshot("branch")
            state = StateProjector().project_visible("branch", events)
            messages = project_chat_messages(events, state, "sys", attachments=branch)
            self.assertIn(str(branch.files.materials), messages[0]["content"])
            self.assertEqual(branch.files.materialize(ref.attachment_id).read_bytes(), b"original")
            await sessions.fork_after_event("branch", event.event_id, "grandchild")
            source_events = await SqliteJournal(sessions.require("source")).snapshot("source")
            await sessions.fork_after_event("source", source_events[0].event_id, "rewound")
            self.assertFalse(
                gateway.for_session("rewound").files.materials.exists()
            )
            self.assertEqual(
                gateway.for_session("grandchild").files.materialize(
                    ref.attachment_id
                ).read_bytes(),
                b"original",
            )

    async def test_edited_message_keeps_attachment_outside_inherited_event_prefix(self):
        with TemporaryDirectory() as directory:
            sessions = SessionStore(Path(directory))
            await sessions.create("source", workspace_id="workspace-test")
            gateway = AttachmentGateway(sessions.root)
            ref = _save(gateway.for_session("source"), b"original", "report.pdf")
            source = AgentRuntime(
                SqliteJournal(sessions.require("source")),
                ScriptedDecisionMaker(()), {}, SequentialIds(),
            )
            target = await source.receive_user_message(
                "source", "[File #1]", delivery_id="input",
                artifact_refs=(ref.attachment_id,),
            )
            edited = await sessions.fork_before_message(
                "source", target.event_id, "edited"
            )
            self.assertEqual(edited.artifact_refs, (ref.attachment_id,))
            self.assertEqual(
                gateway.for_session("edited").read(ref.attachment_id), b"original"
            )
            branch = AgentRuntime(
                SqliteJournal(sessions.require("edited")),
                ScriptedDecisionMaker(()), {}, SequentialIds(),
            )
            new_message = await branch.receive_user_message(
                "edited", "revised [File #1]", delivery_id="input-edited",
                artifact_refs=edited.artifact_refs,
            )
            await sessions.fork_after_event(
                "edited", new_message.event_id, "grandchild"
            )
            self.assertEqual(
                gateway.for_session("grandchild").read(ref.attachment_id),
                b"original",
            )

    async def test_path_admit_does_not_copy_and_survives_fork_via_pointer(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            live = root / "growing.log"
            live.write_bytes(b"head\n")
            sessions = SessionStore(root / "sessions")
            await sessions.create("source", workspace_id="workspace-test")
            gateway = AttachmentGateway(sessions.root)
            source = gateway.for_session("source")
            ref = source.save_file_path(live)
            self.assertEqual(ref.size, 5)
            copied = (
                source.files.originals
                / ref.attachment_id.removeprefix("file:")
                / live.name
            )
            self.assertFalse(copied.exists())
            self.assertEqual(source.path(ref.attachment_id), live.resolve())
            live.write_bytes(b"head\nmore\n")
            self.assertEqual(source.files.describe(ref.attachment_id).size, live.stat().st_size)
            runtime = AgentRuntime(SqliteJournal(sessions.require("source")), ScriptedDecisionMaker(()), {}, SequentialIds())
            event = await runtime.receive_user_message("source", "[File #1]", delivery_id="input", artifact_refs=(ref.attachment_id,))
            await sessions.fork_after_event("source", event.event_id, "branch")
            branch = gateway.for_session("branch")
            self.assertEqual(branch.path(ref.attachment_id), live.resolve())
            self.assertEqual(branch.files.materialize(ref.attachment_id).read_bytes(), live.read_bytes())

    async def test_analysis_files_ignore_image_byte_cap(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = AttachmentGateway(root / "sessions").for_session("session")
            blob = root / "big.bin"
            blob.write_bytes(b"head\n")
            os.truncate(blob, MAX_SOURCE_BYTES + 8)
            with blob.open("rb") as handle:
                streamed = store.save_file_stream(handle, "big.bin")
            mounted = store.save_file_path(blob)
            self.assertGreater(streamed.size, MAX_SOURCE_BYTES)
            self.assertGreater(mounted.size, MAX_SOURCE_BYTES)
            material = store.files.materialize(mounted.attachment_id)
            with material.open("rb") as handle:
                self.assertEqual(handle.read(5), b"head\n")
            original = store.path(streamed.attachment_id)
            linked = store.files.materialize(streamed.attachment_id)
            if original.stat().st_nlink > 1:
                self.assertEqual(original.stat().st_ino, linked.stat().st_ino)

    async def test_bad_external_names_are_rejected_and_missing_originals_propagate(self):
        with TemporaryDirectory() as directory:
            store = AttachmentGateway(Path(directory)).for_session("session")
            for name in ("../outside.txt", "folder/file", "folder\\file", "..", ""):
                with self.subTest(name=name), self.assertRaises(AttachmentRejected):
                    _save(store, b"data", name)
            ref = _save(store, b"", "empty.txt")
            self.assertEqual(ref.size, 0)
            store.path(ref.attachment_id).unlink()
            with self.assertRaisesRegex(ValueError, "只能包含一个文件"):
                store.files.materialize(ref.attachment_id)
            with self.assertRaises(AttachmentRejected):
                store.save_file_path(Path(directory) / "missing.bin")
