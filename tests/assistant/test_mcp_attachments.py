from __future__ import annotations

from base64 import b64encode
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from PIL import Image
from mcp.types import CallToolResult, ImageContent, TextContent

from redpanda.assistant.attachments import AttachmentStore, is_valid_attachment_id
from redpanda.assistant.mcp import extract_images
from redpanda.mcp.adapter import adapt_call_result


def png(size: tuple[int, int] = (8, 8), color: str = "red") -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def mcp_result(*blocks: dict) -> dict:
    return {
        "ok": True,
        "code": "MCP_TOOL_OK",
        "data": {
            "mcp": {
                "content": list(blocks),
                "structured_content": None,
                "meta": None,
            }
        },
    }


def image_block(data: bytes, mime: str = "image/png") -> dict:
    return {
        "type": "image",
        "mimeType": mime,
        "data": b64encode(data).decode("ascii"),
    }


class ExtractImagesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.store = AttachmentStore(Path(self._directory.name))

    def test_image_block_becomes_a_reference_and_base64_disappears(self):
        data = png()
        result = extract_images(mcp_result(image_block(data)), self.store)

        encoded = json.dumps(result)
        self.assertNotIn(b64encode(data).decode("ascii"), encoded)
        reference = result["data"]["mcp"]["content"][0]
        self.assertEqual(reference["type"], "image")
        self.assertTrue(is_valid_attachment_id(reference["id"]))
        self.assertEqual(reference["mime"], "image/png")
        self.assertEqual(self.store.read(reference["id"]), data)

    def test_references_are_hoisted_to_a_top_level_images_list(self):
        result = extract_images(mcp_result(image_block(png())), self.store)

        self.assertEqual(result["images"], [result["data"]["mcp"]["content"][0]])

    def test_text_blocks_are_untouched_and_order_is_preserved(self):
        text = {"type": "text", "text": "before"}
        tail = {"type": "text", "text": "after"}
        result = extract_images(
            mcp_result(text, image_block(png()), tail), self.store
        )

        content = result["data"]["mcp"]["content"]
        self.assertEqual([content[0], content[2]], [text, tail])
        self.assertEqual(len(result["images"]), 1)

    def test_two_identical_images_share_one_stored_object(self):
        data = png()
        result = extract_images(
            mcp_result(image_block(data), image_block(data)), self.store
        )

        first, second = result["images"]
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(list(Path(self._directory.name).iterdir())), 1)

    def test_unsupported_format_is_rejected_visibly_not_dropped(self):
        bmp = BytesIO()
        Image.new("RGB", (8, 8), "red").save(bmp, format="BMP")
        result = extract_images(
            mcp_result(image_block(bmp.getvalue(), "image/bmp")), self.store
        )

        block = result["data"]["mcp"]["content"][0]
        self.assertEqual(block["type"], "image_rejected")
        self.assertEqual(block["mimeType"], "image/bmp")
        self.assertTrue(block["error"])
        self.assertNotIn("images", result)

    def test_malformed_base64_is_rejected_visibly(self):
        block = {"type": "image", "mimeType": "image/png", "data": "not base64 !!"}
        result = extract_images(mcp_result(block), self.store)

        self.assertEqual(
            result["data"]["mcp"]["content"][0]["type"], "image_rejected"
        )

    def test_one_rejection_does_not_discard_the_other_images(self):
        data = png()
        result = extract_images(
            mcp_result(
                {"type": "image", "mimeType": "image/png", "data": "!!"},
                image_block(data),
            ),
            self.store,
        )

        content = result["data"]["mcp"]["content"]
        self.assertEqual(content[0]["type"], "image_rejected")
        self.assertEqual(len(result["images"]), 1)
        self.assertEqual(self.store.read(content[1]["id"]), data)

    def test_result_without_images_is_returned_unchanged(self):
        original = mcp_result({"type": "text", "text": "plain"})

        self.assertIs(extract_images(original, self.store), original)

    def test_transport_error_without_mcp_payload_passes_through(self):
        original = {
            "ok": False,
            "code": "MCP_TRANSPORT_ERROR",
            "data": {},
            "error": "offline",
        }

        self.assertIs(extract_images(original, self.store), original)


class BinaryRedactionTest(unittest.TestCase):
    def test_secret_redaction_skips_binary_blocks(self):
        secret = "s3cr3t-token"
        # base64 里恰好含有密钥子串时，文本替换会破坏原始字节。
        payload = b64encode(png()).decode("ascii")
        adapted = adapt_call_result(
            CallToolResult(
                content=[
                    TextContent(type="text", text=f"token={secret}"),
                    ImageContent(type="image", mimeType="image/png", data=payload),
                ],
                isError=False,
            ),
            secret_values=(secret,),
        )

        content = adapted["data"]["mcp"]["content"]
        self.assertNotIn(secret, content[0]["text"])
        self.assertEqual(content[1]["data"], payload)

    def test_redaction_still_applies_to_text_blocks(self):
        secret = "abcdef"
        adapted = adapt_call_result(
            CallToolResult(
                content=[TextContent(type="text", text=f"x{secret}y")],
                isError=False,
            ),
            secret_values=(secret,),
        )

        self.assertEqual(adapted["data"]["mcp"]["content"][0]["text"], "x***y")
