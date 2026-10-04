from __future__ import annotations

import unittest

from redpanda.llm.images import encode_images


class EncodeImagesTest(unittest.TestCase):
    def test_same_id_is_encoded_once_per_request(self):
        reads: list[str] = []

        def read(attachment_id: str) -> bytes:
            reads.append(attachment_id)
            return b"png-bytes"

        image = {"type": "image", "id": "sha256:" + "a" * 64, "mime": "image/png"}
        encoded = encode_images(
            [
                {"role": "user", "content": [{"type": "text", "text": "one"}, image]},
                {"role": "user", "content": [{"type": "text", "text": "two"}, image]},
            ],
            read,
        )

        self.assertEqual(reads, [image["id"]])
        first = encoded[0]["content"][1]
        second = encoded[1]["content"][1]
        self.assertIs(first, second)
        self.assertTrue(first["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_text_messages_pass_through_without_a_reader(self):
        messages = [{"role": "user", "content": "plain"}]
        self.assertEqual(encode_images(messages, None), messages)

    def test_image_without_reader_is_a_contract_violation(self):
        with self.assertRaises(TypeError):
            encode_images(
                [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "id": "sha256:" + "a" * 64,
                                "mime": "image/png",
                            }
                        ],
                    }
                ],
                None,
            )
