"""用户文件原件与由已提交引用生成的具名材料副本，不负责格式解析。"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
from uuid import uuid4


_FILE_ID = re.compile(r"^file:[0-9a-f]{32}$")


class InvalidAttachmentName(ValueError):
    """上传边界的文件名不适用于本机文件系统。"""


def is_file_attachment_id(value: object) -> bool:
    return type(value) is str and _FILE_ID.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class FileAttachment:
    attachment_id: str
    name: str
    size: int


class FileAttachmentStore:
    def __init__(self, originals: Path, materials: Path):
        self.originals = originals
        self.materials = materials

    def save(self, data: bytes, name: str) -> FileAttachment:
        if (
            type(name) is not str or name in {"", ".", ".."}
            or any(character in name for character in ("/", "\\", "\x00"))
            or (os.name == "nt" and (
                any(ord(character) < 32 or character in ':*?"<>|' for character in name)
                or name.endswith((" ", "."))
                or name.split(".", 1)[0].upper() in {
                    "CON", "PRN", "AUX", "NUL",
                    *(f"COM{i}" for i in range(1, 10)),
                    *(f"LPT{i}" for i in range(1, 10)),
                }
            ))
        ):
            raise InvalidAttachmentName("附件名称必须是有效的单个文件名")
        attachment_id = f"file:{uuid4().hex}"
        directory = self.originals / attachment_id.removeprefix("file:")
        directory.mkdir(parents=True)
        (directory / name).write_bytes(data)
        return FileAttachment(attachment_id, name, len(data))

    def source(self, attachment_id: str) -> Path:
        if not is_file_attachment_id(attachment_id):
            raise ValueError("file attachment id 格式无效")
        directory = self.originals / attachment_id.removeprefix("file:")
        entries = tuple(directory.iterdir())
        if len(entries) != 1 or not entries[0].is_file():
            raise ValueError("附件原件目录必须且只能包含一个文件")
        return entries[0]

    def describe(self, attachment_id: str) -> FileAttachment:
        path = self.source(attachment_id)
        return FileAttachment(attachment_id, path.name, path.stat().st_size)

    def materialize(self, attachment_id: str) -> Path:
        source = self.source(attachment_id)
        directory = self.materials / attachment_id.removeprefix("file:")
        target = directory / source.name
        if not target.is_file():
            directory.mkdir(parents=True, exist_ok=True)
            staging = directory / f".staging-{uuid4().hex}"
            shutil.copyfile(source, staging)
            staging.replace(target)
        return target
