from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from helperme.assistant.attachments import AttachmentGateway, AttachmentRejected
from helperme.assistant.builtin_tools import build_builtin_tools
from helperme.assistant.context.projection import project_chat_messages
from helperme.assistant.conversations import project_conversation
from helperme.assistant.host.session_store import SessionStore
from helperme.assistant.sessions import SessionView
from helperme.runtime import AgentRuntime, MemoryJournal, SqliteJournal, StateProjector
from helperme.sandbox.registry import WorkspaceRecord
from tests.assistant.test_runner import ScriptedDecisionMaker, SequentialIds


class FileAttachmentsTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_committed_files_enter_materials_and_conversation(self):
        with TemporaryDirectory() as directory:
            store = AttachmentGateway(Path(directory)).for_session("session")
            sent = store.save_file(b"original", "中文报告.docx")
            staged = store.save_file(b"draft", "中文报告.docx")
            self.assertNotEqual(sent.attachment_id, staged.attachment_id)
            self.assertFalse(store.files.materials.exists())
            runtime = AgentRuntime(MemoryJournal(), ScriptedDecisionMaker(()), {}, SequentialIds())
            await runtime.receive_user_message(
                "session", "分析 [File #1]", delivery_id="user", artifact_refs=(sent.attachment_id,),
            )
            events = await runtime.snapshot("session")
            state = StateProjector().project_visible("session", events)
            messages = project_chat_messages(events, state, "sys", attachments=store)
            self.assertIn(sent.attachment_id, messages[1]["content"])
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
            ref = store.save_file(b"original", "notes.txt")
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
            ref = source.save_file(b"original", "report.pdf")
            runtime = AgentRuntime(SqliteJournal(sessions.require("source")), ScriptedDecisionMaker(()), {}, SequentialIds())
            event = await runtime.receive_user_message("source", "[File #1]", delivery_id="input", artifact_refs=(ref.attachment_id,))
            source.files.materialize(ref.attachment_id).write_bytes(b"changed material")
            await sessions.fork_after_event("source", event.event_id, "branch")
            branch = gateway.for_session("branch")
            self.assertFalse(branch.files.materials.exists())
            events = await SqliteJournal(sessions.require("branch")).snapshot("branch")
            state = StateProjector().project_visible("branch", events)
            messages = project_chat_messages(events, state, "sys", attachments=branch)
            self.assertIn(str(branch.files.materials), messages[0]["content"])
            self.assertEqual(branch.files.materialize(ref.attachment_id).read_bytes(), b"original")

    async def test_bad_external_names_are_rejected_and_missing_originals_propagate(self):
        with TemporaryDirectory() as directory:
            store = AttachmentGateway(Path(directory)).for_session("session")
            for name in ("../outside.txt", "folder/file", "folder\\file", "..", ""):
                with self.subTest(name=name), self.assertRaises(AttachmentRejected):
                    store.save_file(b"data", name)
            ref = store.save_file(b"", "empty.txt")
            self.assertEqual(ref.size, 0)
            store.path(ref.attachment_id).unlink()
            with self.assertRaisesRegex(ValueError, "只能包含一个文件"):
                store.files.materialize(ref.attachment_id)
