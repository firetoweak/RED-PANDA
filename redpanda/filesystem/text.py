"""UTF-8 file operations on the caller's resolved file view."""
from pathlib import Path
from os.path import commonprefix
from io import DEFAULT_BUFFER_SIZE
import unicodedata

from .lifecycle import settled


async def read_text(path: Path, *, offset: int, limit: int, max_chars: int):
    return await settled(_read_text, path, offset, limit, max_chars)


def _read_text(path, offset, limit, max_chars):
    if not path.exists():
        return {"ok": False, "code": "NOT_FOUND"}
    if not path.is_file():
        return {"ok": False, "code": "NOT_A_FILE"}
    selected, length, last_seen = [], 0, 0
    end_line, next_offset, truncated_by = offset - 1, None, None
    with path.open("r", encoding="utf-8", newline="") as handle:
        for number, line in enumerate(handle, start=1):
            last_seen = number
            if number < offset:
                continue
            if len(selected) >= limit or length + len(line) > max_chars:
                if not selected:
                    return {"ok": False, "code": "LINE_TOO_LONG", "line": number,
                            "preview": line[:max_chars], "max_chars": max_chars}
                next_offset = number
                truncated_by = "lines" if len(selected) >= limit else "chars"
                break
            selected.append(line)
            length += len(line)
            end_line = number
    if not selected and last_seen < offset and not (offset == 1 and last_seen == 0):
        return {"ok": False, "code": "OFFSET_OUT_OF_RANGE"}
    return {"ok": True, "code": "FILE_READ", "content": "".join(selected),
            "start_line": offset, "end_line": end_line, "next_offset": next_offset,
            "truncated": truncated_by is not None, "truncated_by": truncated_by}


async def write_text(path: Path, content: str, *, overwrite: bool):
    return await settled(_write_text, path, content, overwrite)


def _write_text(path, content, overwrite):
    if path.is_dir():
        return {"ok": False, "code": "IS_A_DIR"}
    existed = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("w" if overwrite else "x", encoding="utf-8", newline="") as handle:
            handle.write(content)
    except FileExistsError:
        return {"ok": False, "code": "FILE_EXISTS"}
    return {"ok": True, "code": "FILE_OVERWRITTEN" if existed else "FILE_CREATED"}


async def replace_text(path: Path, old: str, new: str, *, all_matches: bool):
    return await settled(_replace_text, path, old, new, all_matches)


def _replace_text(path, old, new, all_matches):
    if not path.exists():
        return {"ok": False, "code": "NOT_FOUND"}
    if not path.is_file():
        return {"ok": False, "code": "NOT_A_FILE"}
    if not old:
        return {"ok": False, "code": "OLD_BLOCK_EMPTY"}
    before = path.read_bytes()
    content = before.decode("utf-8")
    count = content.count(old)
    if not count:
        if not all_matches:
            lines = content.splitlines(keepends=True)
            width = len(old.splitlines(keepends=True))
            candidates = []
            normalized = normalize_for_match(old)
            for index in range(len(lines) - width + 1):
                block = "".join(lines[index:index + width])
                if normalize_for_match(block) == normalized:
                    candidates.append({"original_block": block})
            if candidates:
                return {"ok": False,
                        "code": "FUZZY_MATCH_ONLY" if len(candidates) == 1 else "FUZZY_MATCH_NOT_UNIQUE",
                        "matches": len(candidates), "candidates": candidates[:3]}
        return {"ok": False, "code": "OLD_BLOCK_NOT_FOUND", "replacements": 0}
    if count > 1 and not all_matches:
        return {"ok": False, "code": "OLD_BLOCK_NOT_UNIQUE", "matches": count}
    after = content.replace(old, new, -1 if all_matches else 1).encode("utf-8")
    if before != after:
        with path.open("r+b") as handle:
            if len(before) == len(after):
                # Separate replacements must not rewrite the gaps between them.
                for offset in range(0, len(after), DEFAULT_BUFFER_SIZE):
                    a, b = before[offset:offset + DEFAULT_BUFFER_SIZE], after[offset:offset + DEFAULT_BUFFER_SIZE]
                    if a == b:
                        continue
                    start = len(commonprefix([a, b]))
                    end = len(b) - len(commonprefix([a[start:][::-1], b[start:][::-1]]))
                    handle.seek(offset + start)
                    handle.write(b[start:end])
            else:
                # Length changes necessarily move the suffix, but never
                # truncate the original before preserving its untouched prefix.
                start = len(commonprefix([before, after]))
                handle.seek(start)
                handle.write(after[start:])
                if len(after) < len(before):
                    handle.truncate(len(after))
    return {"ok": True, "code": "REPLACE_ALL_APPLIED" if all_matches else "PATCH_APPLIED",
            "replacements": count if all_matches else 1}


def normalize_for_match(text: str) -> str:
    return unicodedata.normalize("NFKC", text.replace("\r\n", "\n").replace("\r", "\n"))
