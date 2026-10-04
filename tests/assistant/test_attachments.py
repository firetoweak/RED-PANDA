from __future__ import annotations

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from PIL import Image

from redpanda.assistant.attachments import (
    NORMALIZED_MAX_DIMENSION,
    AttachmentGateway,
    AttachmentRejected,
    AttachmentStore,
    is_valid_attachment_id,
    read_image_binding,
)
from redpanda.assistant.delivery import DELIVER_TOOL_NAME, deliver_binding
from redpanda.runtime import (
    AgentRuntime,
    InvokeTool,
    MemoryJournal,
    ModelDecision,
    ToolBinding,
)
from redpanda.runtime.dispatcher import AttemptContext
from tests.assistant.test_runner import ScriptedDecisionMaker, SequentialIds
from tests.session_scheduler import settle_session


def encode(image: Image.Image, image_format: str) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format=image_format)
    return buffer.getvalue()


def png(size: tuple[int, int] = (8, 8), color: str = "red") -> bytes:
    return encode(Image.new("RGB", size, color), "PNG")


class AttachmentStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.store = AttachmentStore(Path(self._directory.name))

    def test_identical_bytes_share_one_object(self):
        first = self.store.save_image(png(), "image/png")
        second = self.store.save_image(png(), "image/png")

        self.assertEqual(first, second)
        self.assertTrue(is_valid_attachment_id(first.attachment_id))
        self.assertEqual(len(list(Path(self._directory.name).iterdir())), 1)

    def test_different_bytes_get_different_ids(self):
        red = self.store.save_image(png(color="red"), "image/png")
        blue = self.store.save_image(png(color="blue"), "image/png")

        self.assertNotEqual(red.attachment_id, blue.attachment_id)

    def test_saved_bytes_round_trip(self):
        data = png()
        ref = self.store.save_image(data, "image/png")

        self.assertEqual(self.store.read(ref.attachment_id), data)
        inspected = self.store.inspect(ref.attachment_id)
        self.assertEqual(inspected.attachment_id, ref.attachment_id)
        self.assertEqual(inspected.mime, "image/png")
        self.assertEqual((inspected.width, inspected.height), (8, 8))

    def test_oversized_image_is_normalized_and_keeps_source_facts(self):
        ref = self.store.save_image(png((4096, 2048)), "image/png")

        self.assertEqual(ref.width, NORMALIZED_MAX_DIMENSION)
        self.assertEqual(ref.height, NORMALIZED_MAX_DIMENSION // 2)
        self.assertEqual((ref.source_width, ref.source_height), (4096, 2048))
        with Image.open(BytesIO(self.store.read(ref.attachment_id))) as stored:
            self.assertEqual(stored.size, (ref.width, ref.height))

    def test_image_within_limits_is_stored_verbatim(self):
        data = png((320, 240))
        ref = self.store.save_image(data, "image/png")

        self.assertEqual((ref.width, ref.height), (320, 240))
        self.assertEqual((ref.source_width, ref.source_height), (320, 240))
        self.assertEqual(self.store.read(ref.attachment_id), data)

    def test_declared_mime_must_match_actual_format(self):
        with self.assertRaises(AttachmentRejected):
            self.store.save_image(png(), "image/jpeg")

    def test_format_outside_whitelist_is_rejected(self):
        bmp = encode(Image.new("RGB", (8, 8), "red"), "BMP")

        with self.assertRaises(AttachmentRejected):
            self.store.save_image(bmp, "image/bmp")

    def test_undecodable_bytes_are_rejected(self):
        with self.assertRaises(AttachmentRejected):
            self.store.save_image(b"not an image at all", "image/png")

    def test_truncated_image_is_rejected(self):
        data = png((256, 256))

        with self.assertRaises(AttachmentRejected):
            self.store.save_image(data[: len(data) // 2], "image/png")

    def test_missing_object_surfaces_as_corruption(self):
        ref = self.store.save_image(png(), "image/png")
        self.store.path(ref.attachment_id).unlink()

        with self.assertRaises(FileNotFoundError):
            self.store.read(ref.attachment_id)

    def test_invalid_id_is_a_contract_violation(self):
        for candidate in ("", "sha256:zz", "../escape", "sha256:" + "0" * 63):
            self.assertFalse(is_valid_attachment_id(candidate))
            with self.assertRaises(ValueError):
                self.store.path(candidate)


class AttachmentGatewayTest(unittest.TestCase):
    def test_sessions_get_isolated_drawers(self):
        with TemporaryDirectory() as directory:
            gateway = AttachmentGateway(Path(directory))
            first = gateway.for_session("session-a")
            second = gateway.for_session("session-b")
            ref = first.save_image(png(), "image/png")

            self.assertTrue(first.path(ref.attachment_id).is_file())
            self.assertFalse(second.path(ref.attachment_id).exists())

    def test_drawer_sits_beside_the_session_journal(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = AttachmentGateway(root).for_session("session-a")
            ref = store.save_image(png(), "image/png")
            drawer = store.path(ref.attachment_id).parent

            self.assertEqual(drawer.name, ".attachments")
            self.assertEqual(drawer.parent.parent, root.resolve())


class ReadImageTest(unittest.IsolatedAsyncioTestCase):
    async def test_returns_only_ids_that_appeared_in_this_session(self):
        with TemporaryDirectory() as directory:
            store = AttachmentStore(Path(directory))
            owned = store.save_image(png(color="red"), "image/png")
            foreign = store.save_image(png(color="blue"), "image/png")

            async def screenshot(_context, _arguments):
                return {
                    "ok": True,
                    "code": "OK",
                    "data": {},
                    "error": None,
                    "hint": None,
                    "images": [owned.to_block()],
                }

            runtime = AgentRuntime(
                MemoryJournal(),
                ScriptedDecisionMaker(
                    (
                        lambda _frame: ModelDecision(
                            command_requests=(InvokeTool("screenshot"),),
                        ),
                        lambda _frame: ModelDecision(
                            command_requests=(
                                InvokeTool(DELIVER_TOOL_NAME, (("output_id", "output-1"), ("text", "done"))),
                            ),
                        ),
                    )
                ),
                {
                    "screenshot": ToolBinding(screenshot),
                    **deliver_binding(lambda _session_id, _output_id, _text: None),
                },
                SequentialIds(),
            )
            await runtime.receive_user_message("s", "look", delivery_id="u")
            await settle_session(runtime, "s")
            binding = read_image_binding(runtime._journal, store)["read_image"]
            context = AttemptContext("s", "cmd", "att", 1)

            found = await binding.handler(context, {"id": owned.attachment_id})
            self.assertEqual(found["code"], "IMAGE_READ")
            self.assertEqual(found["images"], [owned.to_block()])

            missing = await binding.handler(context, {"id": foreign.attachment_id})
            self.assertEqual(missing["code"], "IMAGE_NOT_IN_SESSION")

    async def test_missing_bytes_surface_as_corruption(self):
        with TemporaryDirectory() as directory:
            store = AttachmentStore(Path(directory))
            owned = store.save_image(png(), "image/png")

            async def screenshot(_context, _arguments):
                return {
                    "ok": True,
                    "code": "OK",
                    "data": {},
                    "images": [owned.to_block()],
                }

            runtime = AgentRuntime(
                MemoryJournal(),
                ScriptedDecisionMaker(
                    (
                        lambda _frame: ModelDecision(
                            command_requests=(InvokeTool("screenshot"),),
                        ),
                        lambda _frame: ModelDecision(
                            command_requests=(
                                InvokeTool(DELIVER_TOOL_NAME, (("output_id", "output-1"), ("text", "done"))),
                            ),
                        ),
                    )
                ),
                {
                    "screenshot": ToolBinding(screenshot),
                    **deliver_binding(lambda _session_id, _output_id, _text: None),
                },
                SequentialIds(),
            )
            await runtime.receive_user_message("s", "look", delivery_id="u")
            await settle_session(runtime, "s")
            store.path(owned.attachment_id).unlink()
            binding = read_image_binding(runtime._journal, store)["read_image"]

            with self.assertRaises(FileNotFoundError):
                await binding.handler(
                    AttemptContext("s", "cmd", "att", 1),
                    {"id": owned.attachment_id},
                )

    async def test_user_message_refs_are_readable(self):
        with TemporaryDirectory() as directory:
            store = AttachmentStore(Path(directory))
            owned = store.save_image(png(), "image/png")
            runtime = AgentRuntime(
                MemoryJournal(),
                ScriptedDecisionMaker(
                    (
                        lambda _frame: ModelDecision(
                            command_requests=(
                                InvokeTool(DELIVER_TOOL_NAME, (("output_id", "output-1"), ("text", "done"))),
                            ),
                        ),
                    )
                ),
                deliver_binding(lambda _session_id, _output_id, _text: None),
                SequentialIds(),
            )
            await runtime.receive_user_message(
                "s",
                "[Image #1]",
                delivery_id="u",
                artifact_refs=(owned.attachment_id,),
            )
            await settle_session(runtime, "s")
            found = await read_image_binding(runtime._journal, store)["read_image"].handler(
                AttemptContext("s", "cmd", "att", 1),
                {"id": owned.attachment_id},
            )
            self.assertEqual(found["code"], "IMAGE_READ")
            self.assertEqual(found["images"][0]["id"], owned.attachment_id)
