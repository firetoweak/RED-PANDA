from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from redpanda.assistant.attachments import AttachmentGateway
from redpanda.channels.web.app import create_web_app
from redpanda.channels.web.channel import WebChannel
from redpanda.channels.web.hub import WebEventHub
from tests.channels.test_web import _Queries, _Sessions


class WebFileAttachmentsTest(unittest.TestCase):
    def test_upload_download_and_send_ordinary_file_is_scoped_to_session(self):
        with TemporaryDirectory() as directory:
            gateway = AttachmentGateway(Path(directory))
            queries = _Queries()
            sessions = _Sessions(queries)
            channel = WebChannel(sessions, queries, gateway)
            connection = channel.connect()
            with TestClient(create_web_app(channel, WebEventHub())) as client:
                response = client.post("/api/sessions/source/attachments", data={"connection_id": connection.connection_id},
                                       files={"file": ("报告.pdf", b"pdf original", "application/pdf")})
                self.assertEqual(response.status_code, 201)
                ref = response.json()
                self.assertEqual((ref["kind"], ref["name"], ref["size"]), ("file", "报告.pdf", 12))
                self.assertFalse(gateway.for_session("source").files.materials.exists())
                download = client.get(f"/api/sessions/source/attachments/{ref['attachment_id']}")
                self.assertEqual(download.content, b"pdf original")
                self.assertIn("attachment;", download.headers["content-disposition"])
                self.assertEqual(client.get(f"/api/sessions/other/attachments/{ref['attachment_id']}").status_code, 404)
                for session, expected in (("other", 400), ("source", 200)):
                    sent = client.post(f"/api/sessions/{session}/inputs", json={"connection_id": connection.connection_id,
                        "delivery_id": f"input-{session}", "text": "[File #1]", "artifact_refs": [ref["attachment_id"]]})
                    self.assertEqual(sent.status_code, expected)
                self.assertEqual(sessions.calls[-1][3]["artifact_refs"], (ref["attachment_id"],))

    def test_attach_local_path_registers_the_live_file_without_copying_bytes(self):
        with TemporaryDirectory() as directory:
            live = Path(directory) / "notes.txt"
            live.write_text("hello path")
            gateway = AttachmentGateway(Path(directory) / "sessions")
            queries = _Queries()
            sessions = _Sessions(queries)
            channel = WebChannel(sessions, queries, gateway)
            connection = channel.connect()
            with TestClient(create_web_app(channel, WebEventHub())) as client:
                response = client.post(
                    "/api/sessions/source/attachments/from-path",
                    json={"connection_id": connection.connection_id, "path": str(live)},
                )
                self.assertEqual(response.status_code, 201)
                ref = response.json()
                self.assertEqual((ref["kind"], ref["name"], ref["size"]), ("file", "notes.txt", 10))
                store = gateway.for_session("source")
                self.assertEqual(store.path(ref["attachment_id"]), live.resolve())
                self.assertFalse(
                    (store.files.originals / ref["attachment_id"].removeprefix("file:") / "notes.txt").exists()
                )
                download = client.get(f"/api/sessions/source/attachments/{ref['attachment_id']}")
                self.assertEqual(download.content, b"hello path")
