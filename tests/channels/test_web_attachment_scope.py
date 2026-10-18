import asyncio
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from redpanda.assistant.attachments import AttachmentGateway, AttachmentNotInSession
from redpanda.assistant.host.session_store import SessionStore
from redpanda.channels.web.app import create_web_app
from redpanda.channels.web.channel import WebChannel
from redpanda.channels.web.hub import WebEventHub
from redpanda.runtime import AgentRuntime, InvokeTool, ModelDecision, SqliteJournal, ToolBinding
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

    async def test_branch_download_and_input_use_actual_inherited_refs(self):
        for kind in ("file", "image"):
            with self.subTest(kind=kind):
                first = self.save("source", kind, "red")
                second = self.save("source", kind, "blue")
                staged = self.save("source", kind, "green")
                runtime = self.runtime("source")
                boundary = await runtime.receive_user_message(
                    "source", "first", delivery_id=f"first-{kind}", artifact_refs=(first.attachment_id,),
                )
                await runtime.receive_user_message(
                    "source", "second", delivery_id=f"second-{kind}", artifact_refs=(second.attachment_id,),
                )
                branch = f"branch-{kind}"
                grandchild = f"grandchild-{kind}"
                await self.store.fork_after_event("source", boundary.event_id, branch)
                await self.store.fork_after_event(branch, boundary.event_id, grandchild)
                own = self.save(branch, kind, "yellow")
                with TestClient(create_web_app(self.channel, WebEventHub())) as client:
                    for sid in (branch, grandchild):
                        self.assertEqual(client.get(
                            f"/api/sessions/{sid}/attachments/{first.attachment_id}",
                        ).status_code, 200)
                        for ref in (second, staged):
                            self.assertEqual(client.get(
                                f"/api/sessions/{sid}/attachments/{ref.attachment_id}",
                            ).status_code, 404)
                            sent = client.post(f"/api/sessions/{sid}/inputs", json={
                                "connection_id": self.connection.connection_id, "delivery_id": "forged",
                                "text": "forged", "artifact_refs": [ref.attachment_id],
                            })
                            self.assertEqual(sent.status_code, 400)
                    for ref in (second, staged):
                        self.assertEqual(client.get(
                            f"/api/sessions/source/attachments/{ref.attachment_id}",
                        ).status_code, 200)
                    self.assertEqual(client.get(
                        f"/api/sessions/{branch}/attachments/{own.attachment_id}",
                    ).status_code, 200)
                    self.assertEqual(client.get(
                        f"/api/sessions/{grandchild}/attachments/{own.attachment_id}",
                    ).status_code, 404)
                self.assertEqual(self.sessions.calls, [])

    async def test_refs_outside_prefix_become_readable_after_explicit_commit(self):
        for kind in ("file", "image"):
            with self.subTest(kind=kind):
                ref = self.save("source", kind, "purple")
                runtime = self.runtime("source")
                boundary = (await runtime.snapshot("source"))[-1]
                await runtime.receive_user_message(
                    "source", "original", delivery_id=f"source-{kind}", artifact_refs=(ref.attachment_id,),
                )
                await runtime.advance("source")
                # A branch can read a source attachment only after explicitly referencing it.
                self.assertEqual(await self.channel._require_refs("source", (ref.attachment_id,)), (ref.attachment_id,))
                branch = f"branch-{kind}"
                await self.store.fork_after_event("source", boundary.event_id, branch)
                with self.assertRaises(AttachmentNotInSession):
                    await self.channel.attachment_file(branch, ref.attachment_id)
                await self.runtime(branch).receive_user_message(
                    branch, "referenced", delivery_id="reference", artifact_refs=(ref.attachment_id,),
                )
                path, _ = await self.channel.attachment_file(branch, ref.attachment_id)
                self.assertEqual(path, self.gateway.for_session("source").path(ref.attachment_id))
                self.assertEqual(await self.channel._require_refs(branch, (ref.attachment_id,)), (ref.attachment_id,))

    async def test_missing_registered_original_is_not_treated_as_out_of_scope(self):
        live = Path(self.directory.name) / "live.txt"
        live.write_text("original")
        ref = self.gateway.for_session("source").save_file_path(live)
        image = self.save("source", "image", "black")
        message = await self.runtime("source").receive_user_message(
            "source", "files", delivery_id="files", artifact_refs=(ref.attachment_id, image.attachment_id),
        )
        await self.store.fork_after_event("source", message.event_id, "branch")
        live.unlink()
        self.gateway.for_session("source").path(image.attachment_id).unlink()
        with TestClient(create_web_app(self.channel, WebEventHub())) as client:
            for sid in ("source", "branch"):
                for item in (ref, image):
                    self.assertTrue(await self.gateway.available_in_session(sid, item.attachment_id))
                    with self.assertRaises(FileNotFoundError):
                        await self.channel.attachment_file(sid, item.attachment_id)
                    with self.assertRaises(FileNotFoundError):
                        await self.channel._require_refs(sid, (item.attachment_id,))
                    with self.assertRaises(FileNotFoundError):
                        client.get(f"/api/sessions/{sid}/attachments/{item.attachment_id}")

    async def test_inherited_tool_image_uses_outcome_reference(self):
        ref = self.save("source", "image", "orange")

        async def screenshot(_context, _arguments):
            return {"ok": True, "images": [ref.to_block()]}

        runtime = AgentRuntime(
            SqliteJournal(self.store.require("source")),
            ScriptedDecisionMaker((lambda _: ModelDecision(command_requests=(InvokeTool("screenshot"),)),)),
            {"screenshot": ToolBinding(screenshot)},
        )
        user = await runtime.receive_user_message("source", "look", delivery_id="look")
        await runtime.advance("source")
        while runtime.dispatcher.active_count:
            await asyncio.sleep(0)
        events = await runtime.snapshot("source")
        await self.store.fork_after_event("source", user.event_id, "before-shot")
        await self.store.fork_after_event("source", events[-1].event_id, "after-shot")
        with self.assertRaises(AttachmentNotInSession):
            await self.channel.attachment_file("before-shot", ref.attachment_id)
        path, mime = await self.channel.attachment_file("after-shot", ref.attachment_id)
        self.assertEqual(mime, "image/png")
        self.assertEqual(path, self.gateway.for_session("source").path(ref.attachment_id))
