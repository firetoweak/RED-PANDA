"""One model-facing reader for referenced original text."""
from __future__ import annotations

from collections.abc import Mapping

from redpanda.assistant.artifacts import (
    ArtifactStore, ArtifactNotFoundError,
    ArtifactOffsetOutOfRangeError, is_valid_artifact_id,
)

READ_CONTENT = "read_content"
DEFAULT_READ_CHARS = 12_000
MAX_READ_CHARS = 32_000
SEARCH_FRAGMENT_CHARS = 1_200
SEARCH_CONTEXT_CHARS = 160
CONTENT_HINT = (
    "本次工具返回的原文可用 read_content(reference=返回的引用) 读取；"
    "query 按区分大小写的字面关键词定位并返回附近原文，省略 query 按字符范围读取。"
    "可用片段的 offset 向前扩读，next_offset 继续读取或查找。"
    "若原工具提示检索或读取范围不完整，范围外的内容仍需重新检索或读取。"
)

READ_CONTENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": READ_CONTENT,
        "description": (
            "读取工具结果或历史检索提供的真实 reference。"
            "省略 query 从 offset 读取连续原文；query 非空时从 offset 查找字面关键词，"
            "直接返回命中附近原文。offset 从 0 开始，所有位置按字符计。"
            "limit 是本次所有片段的正文总量，默认 12000、最大 32000。"
            "片段的 offset/end_offset 表示原文范围（不含末端），可用于扩读。"
            "next_offset 非空表示还有内容或匹配；续查沿用 reference、query。"
            "空 fragments 表示本次范围没有匹配，不代表其他内容不存在。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reference": {"type": "string", "pattern": r"^art_[0-9a-f]{32}$"},
                "query": {"type": "string", "default": "", "description": "字面关键词；空串表示连续读取。"},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_READ_CHARS, "default": DEFAULT_READ_CHARS},
            },
            "required": ["reference"],
            "additionalProperties": False,
        },
    },
}


def read_content_result(store: ArtifactStore, arguments: Mapping[str, object]) -> dict:
    reference = arguments.get("reference")
    offset = arguments.get("offset", 0)
    limit = arguments.get("limit", DEFAULT_READ_CHARS)
    query = arguments.get("query", "")
    if (
        set(arguments) - {"reference", "offset", "limit", "query"}
        or not is_valid_artifact_id(reference)
        or type(offset) is not int or offset < 0
        or type(limit) is not int or not 1 <= limit <= MAX_READ_CHARS
        or type(query) is not str or len(query) > limit
    ):
        return {"ok": False, "code": "INVALID_ARGUMENT", "error": "提供真实 reference、非负 offset、1 到 32000 的 limit；query 长度不能超过 limit。"}
    try:
        if not query:
            chunk = store.read(reference, offset, limit)
            end = offset + len(chunk.content)
            fragments = [{"offset": offset, "end_offset": end, "content": chunk.content}]
            next_offset = chunk.next_offset
            total = chunk.total_chars
        else:
            total = store.read(reference, offset, 1).total_chars
            fragments = []
            remaining = limit
            cursor = offset
            last_end = offset
            match = store.find(reference, query, cursor)
            while match >= 0 and remaining >= len(query):
                before = min(SEARCH_CONTEXT_CHARS, remaining - len(query))
                start = max(offset, min(match, max(last_end, match - before)))
                length = min(remaining, max(SEARCH_FRAGMENT_CHARS, match - start + len(query)))
                chunk = store.read(reference, start, length)
                end = start + len(chunk.content)
                fragments.append({"offset": start, "end_offset": end, "content": chunk.content, "match_offset": match})
                remaining -= len(chunk.content)
                last_end = end
                # Search again where a match could cross the displayed boundary.
                cursor = max(match + len(query), end - len(query) + 1)
                match = store.find(reference, query, cursor)
            next_offset = match if match >= 0 else None
    except ArtifactNotFoundError:
        return {"ok": False, "code": "CONTENT_NOT_FOUND", "error": "该引用不在当前可读内容中。"}
    except ArtifactOffsetOutOfRangeError as error:
        return {"ok": False, "code": "OFFSET_OUT_OF_RANGE", "error": str(error)}
    return {
        "ok": True, "code": "CONTENT_READ",
        "data": {"reference": reference, "fragments": fragments, "total_chars": total, "next_offset": next_offset},
        "hint": "需要附近更多原文时，省略 query，调整 offset 和 limit；继续查找时沿用 query 和 next_offset。",
    }
