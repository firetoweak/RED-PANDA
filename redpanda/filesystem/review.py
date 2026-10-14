"""Read-only, operation-scoped review of remaining file effects."""
from difflib import SequenceMatcher
from hashlib import sha256
import json

from .changes import _identity_bindings, _observe_file, _origin, _ranges, operation_changes, original_complete


PAGE_LINES = 200


def _review_file(root, store, path, changes, owners, identities):
    metadata, blocks, _current, data, missing, appended = _observe_file(
        root, store, path, changes, identities, full_content=True, owners=owners)
    agent = any(1 in block.owners for block in blocks.values()) or any(
        value.owner == 1 for value in metadata.values())
    if not agent:
        return None
    external = appended > 0 or any(2 in block.owners for block in blocks.values()) or any(
        value.owner == 2 for value in metadata.values())
    chunk_size = changes[0]["before"]["chunk_size"]
    incomplete_original = not original_complete(metadata, blocks, chunk_size)
    current_bytes = b"".join(data[key] for key in sorted(data, key=int))
    rows, added, removed = [], 0, 0
    partial = bool(missing or appended or external or incomplete_original)
    binary = False
    old_prefix, old_line = 0, 1
    for group in _ranges(blocks, chunk_size):
        start = group[0][0] * chunk_size
        before = b"".join(value.baseline for _, value in group)
        after = b"".join(value.current for _, value in group)
        if start != old_prefix:
            old_line = None
        old_prefix = start + len(before)
        if before == after:
            if old_line is not None:
                old_line += before.count(b"\n")
            continue
        try:
            old, new = before.decode("utf-8"), after.decode("utf-8")
        except UnicodeDecodeError:
            binary = True
            continue
        if "\0" in old or "\0" in new:
            binary = True
            continue
        new_line = 1 + current_bytes[:start].count(b"\n")
        old_lines, new_lines = old.splitlines(keepends=True), new.splitlines(keepends=True)
        old_offsets, new_offsets = [0], [0]
        for line in old_lines:
            old_offsets.append(old_offsets[-1] + len(line.encode("utf-8")))
        for line in new_lines:
            new_offsets.append(new_offsets[-1] + len(line.encode("utf-8")))
        owners = b"".join(bytes(value.owners).ljust(chunk_size, b"\0") for _, value in group)
        for opcodes in SequenceMatcher(None, old_lines, new_lines).get_grouped_opcodes(3):
            first, last = opcodes[0], opcodes[-1]
            old_start = None if old_line is None else old_line + first[1]
            rows.append(dict(kind="hunk", old_line=None, new_line=None, origin="unknown",
                             text=f"@@ -{old_start if old_start is not None else '?'},{last[2] - first[1]} +{new_line + first[3]},{last[4] - first[3]} @@"))
            for tag, i, end_i, j, end_j in opcodes:
                origin = "unknown" if tag == "equal" else _origin(
                    owners[old_offsets[i]:old_offsets[end_i]] + owners[new_offsets[j]:new_offsets[end_j]])
                if tag != "equal":
                    if origin == "agent":
                        removed += end_i - i
                        added += end_j - j
                    else:
                        partial = True
                if tag == "equal":
                    for offset in range(end_i - i):
                        rows.append(dict(kind="context", old_line=None if old_line is None else old_line + i + offset,
                                         new_line=new_line + j + offset, origin=origin, text=old_lines[i + offset]))
                else:
                    for offset in range(i, end_i):
                        rows.append(dict(kind="delete", old_line=None if old_line is None else old_line + offset,
                                         new_line=None, origin=origin, text=old_lines[offset]))
                    for offset in range(j, end_j):
                        rows.append(dict(kind="add", old_line=None, new_line=new_line + offset,
                                         origin=origin, text=new_lines[offset]))
        if old_line is not None:
            old_line += before.count(b"\n")
    names = {"kind": "文件类型", "size": "大小（字节）", "mode": "权限", "hidden": "隐藏属性", "readonly": "只读属性", "target": "链接目标"}
    kinds = {"missing": "不存在", "file": "文件", "directory": "目录", "symlink": "符号链接"}
    properties = []
    for name, value in metadata.items():
        if not value.owner:
            continue
        if name == "identity":
            if value.baseline is None or value.current is None:
                continue
            text = "文件对象已替换"
        else:
            before = kinds[value.baseline] if name == "kind" else value.baseline
            after = kinds[value.current] if name == "kind" else value.current
            text = f"{names[name]}：{before} → {after}"
        properties.append(dict(origin=_origin([value.owner]), text=text))
    kind = metadata["kind"]
    status = "added" if kind.owner == 1 and kind.baseline == "missing" else (
        "deleted" if kind.owner == 1 and kind.current == "missing" else "modified")
    # A sparse observation may cut a line at either end. Do not report its
    # fragments as exact line counts, even when their ownership is known.
    exact = not (missing or appended or binary or partial)
    result = dict(path=path[1:], status=status, origin="mixed" if external else "agent",
                  added=added if exact else None, removed=removed if exact else None,
                  partial=not exact, binary=binary, properties=properties, lines=rows)
    fingerprint = json.dumps([result, [(key, value.baseline.hex(), value.current.hex(), bytes(value.owners).hex())
                                      for key, value in sorted(blocks.items())]], ensure_ascii=False, sort_keys=True)
    result["revision"] = sha256(fingerprint.encode()).hexdigest()
    return result


def review_changes(root, store, operations, *, path=None, offset=0):
    grouped, receipts = operation_changes(store, operations)
    by_path = {name: observations for name, observations in grouped.items()
               if path is None or name == "/" + path}
    identities = _identity_bindings(store, receipts) if by_path else {}
    files = []
    for name, observations in sorted(by_path.items()):
        changes, owners = zip(*observations)
        result = _review_file(root, store, name, changes, owners, identities)
        if result is not None:
            files.append(result)
    if path is not None:
        if not files:
            return None
        result = files[0]
        lines = result["lines"]
        return {**result, "lines": lines[offset:offset + PAGE_LINES],
                "next_offset": offset + PAGE_LINES if offset + PAGE_LINES < len(lines) else None}
    return dict(files=[{key: value for key, value in file.items() if key not in ("lines", "properties")}
                       for file in files],
                added=sum(file["added"] for file in files if file["added"] is not None),
                removed=sum(file["removed"] for file in files if file["removed"] is not None),
                partial=any(file["partial"] for file in files))
