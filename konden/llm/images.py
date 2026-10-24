"""把模型接口内的本地图片引用转换为 Chat Completions 内容块。"""

from __future__ import annotations

from base64 import b64encode
from collections.abc import Callable


def encode_images(
    messages: list[dict[str, object]],
    read: Callable[[str], bytes] | None,
) -> list[dict[str, object]]:
    encoded: dict[str, dict[str, object]] = {}
    result: list[dict[str, object]] = []
    for message in messages:
        content = message.get("content")
        if type(content) is not list:
            result.append(message)
            continue
        parts = []
        for part in content:
            if part["type"] != "image":
                parts.append(part)
                continue
            if read is None:
                raise TypeError("image parts require an attachment reader")
            attachment_id = part["id"]
            if attachment_id not in encoded:
                data = b64encode(read(attachment_id)).decode("ascii")
                encoded[attachment_id] = {
                    "type": "image_url",
                    "image_url": {"url": f"data:{part['mime']};base64,{data}"},
                }
            parts.append(encoded[attachment_id])
        result.append({**message, "content": parts})
    return result
