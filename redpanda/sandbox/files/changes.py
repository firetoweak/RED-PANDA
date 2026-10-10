"""Read current attribution from published operation evidence, without changing it."""
from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from itertools import accumulate
import os
from pathlib import Path, PurePosixPath
import stat

from .file_view import native
from .file_view.publication import (
    Conflict, blob, image, read_index, target, transaction_record, valid_image, valid_operation,
)
from .state import fields, load

MAX_FILES = 100
MAX_CONTENT_CHARS = 12_000


async def read_changes(path: Path, *, filesystem=None, offset: int = 0, text_offset: int = 0) -> dict:
    if filesystem is None:
        return {"ok": False, "code": "CHANGES_UNAVAILABLE",
                "error": "此路径无法核对助手修改，请读取相关文件确认当前内容。"}
    return await filesystem.changes(path, offset=offset, text_offset=text_offset)


def _origin(owners):
    agent, external = 1 in owners, 2 in owners
    return "mixed" if agent and external else "agent" if agent else "external" if external else "unknown"


def _ranges(blocks, chunk_size):
    """Join contiguous observations without inventing missing original bytes."""
    group = []
    for key in sorted(blocks, key=int):
        value = blocks[key]
        if group:
            previous, last = group[-1]
            if (int(key) != previous + 1
                    or (len(last.baseline) < chunk_size and value.baseline)
                    or (len(last.current) < chunk_size and value.current)):
                yield group
                group = []
        group.append((int(key), value))
    if group:
        yield group


def _edits(blocks, chunk_size, budget, text_offset=0):
    edits, limitations, truncated = [], [], text_offset > 0
    position = 0
    for group in _ranges(blocks, chunk_size):
        before = b"".join(value.baseline for _, value in group)
        after = b"".join(value.current for _, value in group)
        if before == after:
            continue
        try:
            old, new = before.decode("utf-8"), after.decode("utf-8")
        except UnicodeDecodeError:
            limitations.append("部分变化不是完整的 UTF-8 文本，未展示其正文。")
            continue
        if "\0" in old or "\0" in new:
            limitations.append("二进制变化未展示正文。")
            continue
        owners = b"".join(bytes(value.owners).ljust(chunk_size, b"\0") for _, value in group)
        old_lines, new_lines = old.splitlines(keepends=True), new.splitlines(keepends=True)
        old_offsets = [0, *accumulate(len(line.encode("utf-8")) for line in old_lines)]
        new_offsets = [0, *accumulate(len(line.encode("utf-8")) for line in new_lines)]
        for i, end_i, j, end_j in _text_changes(old_lines, new_lines):
            a, b = "".join(old_lines[i:end_i]), "".join(new_lines[j:end_j])
            width = max(len(a), len(b))
            if position + width <= text_offset:
                position += width
                continue
            start = max(0, text_offset - position)
            remaining_a, remaining_b = max(0, len(a) - start), max(0, len(b) - start)
            smaller = min(remaining_a, remaining_b)
            count = min(max(remaining_a, remaining_b),
                        budget // 2 if budget < 2 * smaller else budget - smaller)
            if count == 0 or len(edits) == 100:
                return edits, limitations, True, budget, max(text_offset, position)
            origin = _origin(owners[old_offsets[i]:old_offsets[end_i]]
                             + owners[new_offsets[j]:new_offsets[end_j]])
            if origin == "unknown":
                limitations.append("部分文本变化来源无法判断。")
            end = start + count
            cut = start > 0 or end < width
            byte_offset = group[0][0] * chunk_size + new_offsets[j] + len(b[:start].encode("utf-8"))
            a, b = a[start:end], b[start:end]
            edits.append({"origin": origin,
                          "byte_offset": byte_offset, "text_offset": start,
                          "before": a, "after": b, "truncated": cut})
            budget -= len(a) + len(b)
            truncated |= cut
            if end < width:
                return edits, limitations, True, budget, position + end
            position += width
    return edits, list(dict.fromkeys(limitations)), truncated, budget, None


def _text_changes(old_lines, new_lines):
    for tag, i, end_i, j, end_j in SequenceMatcher(None, old_lines, new_lines).get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace" and end_i - i == end_j - j:
            # Adjacent line edits can have different origins. Keep them separate.
            for offset in range(end_i - i):
                yield i + offset, i + offset + 1, j + offset, j + offset + 1
        else:
            yield i, end_i, j, end_j


class ChangesReadConflict(OSError):
    """A path changed while its current state was being read."""


@dataclass
class _Value:
    baseline: object
    current: object
    owner: int = 0

    def observe(self, value, owner):
        if value != self.current:
            self.owner = 0 if value == self.baseline else owner
            self.current = value


@dataclass
class _Block:
    baseline: bytes
    current: bytes
    owners: bytearray = field(default_factory=bytearray)

    def observe(self, value: bytes, owner: int):
        if value == self.current:
            return
        length = max(len(self.baseline), len(self.current), len(value))
        self.owners.extend(b"\0" * (length - len(self.owners)))
        for index in range(max(len(self.current), len(value))):
            old = self.current[index] if index < len(self.current) else None
            new = value[index] if index < len(value) else None
            if old != new:
                original = self.baseline[index] if index < len(self.baseline) else None
                self.owners[index] = 0 if new == original else owner
        self.current = value


def _bytes(store, value, key):
    if value["kind"] != "file" or int(key) * value["chunk_size"] >= value["size"]:
        return b""
    try:
        return blob(store, value, key) if key in value["blocks"] else None
    except FileNotFoundError as exc:
        raise ValueError("operation block evidence is missing") from exc


def _stamp(info):
    # Windows fstat and lstat expose different ctime meanings; compare the
    # object identity, content timestamp, size and mode shared by both APIs.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode


def _current(root, path, chunk_size, keys):
    """Read only observed blocks; keep current bytes out of the evidence store."""
    opened = False
    try:
        native_path = target(root, path)
    except Conflict as exc:
        raise ChangesReadConflict("路径含不支持的链接，无法核对文件变化") from exc
    try:
        info = native_path.lstat()
        if stat.S_ISLNK(info.st_mode):
            link = os.readlink(native_path)
            current = image("symlink", len(os.fsencode(link)), native.directory_identity(native_path),
                            chunk_size, mode=stat.S_IMODE(info.st_mode), target=link)
            if _stamp(info) != _stamp(native_path.lstat()):
                raise ChangesReadConflict("文件在查询期间发生变化，请重新查询")
            return current, {}
        if stat.S_ISDIR(info.st_mode):
            current = image("directory", identity=native.directory_identity(native_path), chunk_size=chunk_size)
            if _stamp(info) != _stamp(native_path.lstat()):
                raise ChangesReadConflict("目录在查询期间发生变化，请重新查询")
            return current, {}
        if not stat.S_ISREG(info.st_mode):
            raise ChangesReadConflict("该路径不是普通文件、目录或符号链接")
        with native.open_file(native_path, shared_read=True) as file:
            opened = True
            before = os.fstat(file.fileno())
            identity = native.identity(file)
            blocks = {}
            for key in keys:
                offset = int(key) * chunk_size
                file.seek(offset)
                blocks[key] = file.read(max(0, min(chunk_size, before.st_size - offset)))
            if _stamp(before) != _stamp(os.fstat(file.fileno())) or _stamp(before) != _stamp(native_path.lstat()):
                raise ChangesReadConflict("文件在查询期间发生变化，请重新查询")
            return image("file", before.st_size, identity, chunk_size, False,
                         mode=None if os.name == "nt" else stat.S_IMODE(before.st_mode)), blocks
    except (FileNotFoundError, NotADirectoryError):
        # A disappearance after opening is a race, not a stable missing file.
        if opened:
            raise ChangesReadConflict("文件在查询期间被移除，请重新查询") from None
        return image(chunk_size=chunk_size), {}


def _receipt(store, identity):
    try:
        receipt = load(store / "commands" / identity / "sealed.json")
    except FileNotFoundError as exc:
        raise ValueError("published operation evidence is missing") from exc
    fields(receipt, ("version", "command_id", "parent_commit", "digest", "changes", "operation"))
    if receipt["version"] != 3 or receipt["command_id"] != identity:
        raise ValueError("invalid published receipt")
    valid_operation(receipt["operation"])
    for change in receipt["changes"]:
        fields(change, ("path", "before", "after"))
        name = PurePosixPath(change["path"])
        if (not name.is_absolute() or name.as_posix() != change["path"] or ".." in name.parts
                or name == PurePosixPath("/") or any(c in change["path"] for c in "\\:\0")):
            raise ValueError("invalid evidence path")
        valid_image(change["before"])
        valid_image(change["after"])
    return receipt


def _identity_bindings(store, receipts):
    bindings = {}
    for path in (store / "host").glob("*.json"):
        transaction = transaction_record(load(path))
        if not transaction["finalized"]:
            raise RuntimeError("unfinished host transaction requires reconciliation")
        latest = {}
        if transaction["kind"] == "publish":
            for command in transaction["commands"]:
                if command not in receipts:
                    receipts[command] = _receipt(store, command)
                for change in receipts[command]["changes"]:
                    latest[change["path"]] = change["after"]["identity"]
        for step in transaction["steps"]:
            if step["desired"]["kind"] == "missing":
                continue
            physical = step["created_identity"] or step["expected"]["identity"]
            logical = latest[step["path"]] if transaction["kind"] == "publish" else step["desired"]["identity"]
            if physical is None:
                raise ValueError("published object identity is missing")
            if logical is not None and logical != physical:
                if bindings.setdefault(logical, physical) != physical:
                    raise ValueError("conflicting object identity binding")
    for logical in bindings:
        current, seen = logical, set()
        while current in bindings and bindings[current] != current:
            if current in seen:
                raise ValueError("cyclic object identity bindings")
            seen.add(current)
            current = bindings[current]
        bindings[logical] = current
    return bindings


def _file_state(root, store, path, changes, identities, budget, text_offset=0):
    metadata = {}
    blocks = {}
    chunk_size = changes[0]["before"]["chunk_size"]
    for change in changes:
        before, after = change["before"], change["after"]
        if before["chunk_size"] != chunk_size or after["chunk_size"] != chunk_size:
            raise ValueError("block geometry differs")
        for name in ("kind", "size", "mode", "target", "identity"):
            previous, following = before[name], after[name]
            if name == "identity":
                previous = identities.get(previous, previous)
                following = identities.get(following, following)
            value = metadata.setdefault(name, _Value(previous, previous))
            value.observe(previous, 2)
            value.observe(following, 1)
        keys = before["blocks"].keys() | after["blocks"].keys() | blocks.keys()
        for key in keys:
            previous, following = _bytes(store, before, key), _bytes(store, after, key)
            if key not in blocks:
                if previous is None or following is None:
                    raise ValueError("operation block evidence is incomplete")
                blocks[key] = _Block(previous, previous)
            if previous is not None:
                blocks[key].observe(previous, 2)
            if following is not None:
                blocks[key].observe(following, 1)
    current, data = _current(root, path, chunk_size, sorted(blocks, key=int))
    for name, value in metadata.items():
        value.observe(current[name], 2)
    for key, value in blocks.items():
        value.observe(data[key] if current["kind"] == "file" else b"", 2)
    observed_bytes = sum(len(value) for value in data.values())
    unobserved_bytes = current["size"] - observed_bytes if current["kind"] == "file" else 0
    # Bytes beyond every observed file length were absent in both the baseline
    # and every assistant result. Their external origin needs no content read.
    extent = max((value["size"] for change in changes
                  for value in (change["before"], change["after"]) if value["kind"] == "file"), default=0)
    appended = max(0, current["size"] - extent) if current["kind"] == "file" else 0
    for key, value in data.items():
        start = int(key) * chunk_size
        appended -= max(0, start + len(value) - max(start, extent))
    unobserved_bytes -= appended
    agent = any(1 in value.owners for value in blocks.values()) or any(value.owner == 1 for value in metadata.values())
    external = (appended > 0 or any(2 in value.owners for value in blocks.values())
                or any(value.owner == 2 for value in metadata.values()))
    origin = "mixed" if agent and external else "agent" if agent else "external" if external else "unknown" if unobserved_bytes else "unchanged"
    edits, limitations, truncated, budget, next_text_offset = _edits(blocks, chunk_size, budget, text_offset)
    properties = []
    names = {"kind": "类型", "size": "大小（字节）", "mode": "权限", "target": "链接目标"}
    for name, value in metadata.items():
        if not value.owner:
            continue
        if name == "identity":
            if value.baseline is None or value.current is None:
                continue  # Creation and deletion are already described by kind.
            description = "文件对象被替换"
        else:
            description = f"{names[name]}：{value.baseline} → {value.current}"
        properties.append({"origin": _origin([value.owner]), "description": description})
    if unobserved_bytes:
        limitations.append("文件有未观察的内容，结果仅覆盖有原值可比较的部分。")
    if appended:
        limitations.append("外部追加内容尚未展示，请读取当前文件核对。")
    if truncated:
        limitations.append("这里只展示部分修改前后内容，请结合各页核对；有续读位置时继续读取。")
    return {
        "path": path[1:], "origin": origin, "edits": edits, "properties": properties,
        "content_complete": not (unobserved_bytes or limitations or truncated),
        "limitations": limitations, "truncated": truncated,
        "next_text_offset": next_text_offset,
    }, budget


def attribution(root: Path, store: Path, path: Path, *, offset=0, text_offset=0):
    relative = path.relative_to(root).as_posix()
    selected = PurePosixPath("/" if relative == "." else "/" + relative)
    index = read_index(store)
    by_path = {}
    receipts = {}
    for identity in index["active"]:
        receipt = receipts[identity] = _receipt(store, identity)
        for change in receipt["changes"]:
            name = PurePosixPath(change["path"])
            if name.is_relative_to(selected):
                by_path.setdefault(change["path"], []).append(change)
    identities = _identity_bindings(store, receipts) if by_path else {}
    if text_offset and (len(by_path) != 1 or selected.as_posix() not in by_path):
        return {"ok": False, "code": "TEXT_OFFSET_REQUIRES_FILE",
                "error": "续读文本时，请把 path 指定为需要核对的单个文件。"}
    files, budget = [], MAX_CONTENT_CHARS
    for name in sorted(by_path)[offset:offset + MAX_FILES]:
        state, budget = _file_state(root, store, name, by_path[name], identities, budget, text_offset)
        files.append(state)
    next_offset = offset + len(files) if len(by_path) > offset + len(files) else None
    truncated = next_offset is not None or any(item["truncated"] for item in files)
    return {
        "ok": True, "code": "CHANGES_READ", "scope": "agent_touched_files",
        "changes": files, "truncated": truncated,
        "next_offset": next_offset,
        "content_complete": not (offset or text_offset or truncated) and all(item["content_complete"] for item in files),
    }
