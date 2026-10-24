import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from konden.assistant.attachments import AttachmentGateway
from konden.assistant.host.session_store import SessionStore
from konden.channels.web.app import create_web_app
from konden.channels.web.channel import WebChannel
from konden.channels.web.hub import WebEventHub
from konden.runtime import (
    AgentRuntime,
    InvokeTool,
    ModelDecision,
    SqliteJournal,
    ToolBinding,
)
from tests.assistant.test_attachments import png
from tests.assistant.test_runner import ScriptedDecisionMaker
from tests.channels.test_web import _Queries, _Sessions


class WebAttachmentScopeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.store = SessionStore(Path(self.directory.name) / "sessions")
        await self.store.create("source", workspace_id="workspace-test")
        self.gateway = AttachmentGateway(self.store.root)
        self.queries = _Queries()
        self.sessions = _Sessions(self.queries)
        self.channel = WebChannel(self.sessions, self.queries, self.gateway)

    async def asyncTearDown(self):
        self.directory.cleanup()

    def runtime(self, session_id):
        return AgentRuntime(
            SqliteJournal(self.store.require(session_id)),
            ScriptedDecisionMaker((lambda _: ModelDecision(content="done"),)), {},
        )

    async def test_tool_result_image_is_available_in_its_session_history(self):
        store = self.gateway.for_session("source")
        ref = store.save_image(png(color="orange"), "image/png")

        async def screenshot(_context, _arguments):
            return {"ok": True, "images": [ref.to_block()]}

        runtime = AgentRuntime(
            SqliteJournal(self.store.require("source")),
            ScriptedDecisionMaker((lambda _: ModelDecision(command_requests=(InvokeTool("screenshot"),)),)),
            {"screenshot": ToolBinding(screenshot)},
        )
        await runtime.receive_user_message("source", "look", delivery_id="look")
        await runtime.advance("source")
        while runtime.dispatcher.active_count:
            await asyncio.sleep(0)
        # Force the history path, rather than the shortcut for existing local bytes.
        store.path(ref.attachment_id).unlink()
        self.assertTrue(await self.gateway.available_in_session("source", ref.attachment_id))
        self.assertFalse(await self.gateway.available_in_session("other", ref.attachment_id))

    async def test_missing_registered_original_is_not_treated_as_out_of_scope(self):
        live = Path(self.directory.name) / "live.txt"
        live.write_text("original")
        ref = self.gateway.for_session("source").save_file_path(live)
        image = self.gateway.for_session("source").save_image(png(color="black"), "image/png")
        await self.runtime("source").receive_user_message(
            "source", "files", delivery_id="files", artifact_refs=(ref.attachment_id, image.attachment_id),
        )
        live.unlink()
        self.gateway.for_session("source").path(image.attachment_id).unlink()
        with TestClient(create_web_app(self.channel, WebEventHub())) as client:
            for item in (ref, image):
                self.assertTrue(await self.gateway.available_in_session("source", item.attachment_id))
                with self.assertRaises(FileNotFoundError):
                    await self.channel.attachment_file("source", item.attachment_id)
                with self.assertRaises(FileNotFoundError):
                    await self.channel._require_refs("source", (item.attachment_id,))
                with self.assertRaises(FileNotFoundError):
                    client.get(f"/api/sessions/source/attachments/{item.attachment_id}")
