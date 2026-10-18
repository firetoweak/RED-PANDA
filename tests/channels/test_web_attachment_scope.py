from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from redpanda.assistant.attachments import AttachmentGateway
from redpanda.assistant.host.session_store import SessionStore
from redpanda.channels.web.app import create_web_app
from redpanda.channels.web.channel import WebChannel
from redpanda.channels.web.hub import WebEventHub
from redpanda.runtime import AgentRuntime, ModelDecision, SqliteJournal
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
        self.connection = self.channel.connect()

    async def asyncTearDown(self):
        self.directory.cleanup()

    def runtime(self, session_id):
        return AgentRuntime(
            SqliteJournal(self.store.require(session_id)),
            ScriptedDecisionMaker((lambda _: ModelDecision(content="done"),)), {},
        )

    def save(self, session_id, kind, value):
        drawer = self.gateway.for_session(session_id)
        if kind == "image":
            return drawer.save_image(png(color=value), "image/png")
        return drawer.save_file_stream(BytesIO(value.encode()), "report.txt")

    async def test_missing_registered_original_is_not_treated_as_out_of_scope(self):
        live = Path(self.directory.name) / "live.txt"
        live.write_text("original")
        ref = self.gateway.for_session("source").save_file_path(live)
        image = self.save("source", "image", "black")
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
