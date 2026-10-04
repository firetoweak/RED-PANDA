"""用户文件原件与由已提交引用生成的具名材料视图，不负责格式解析。"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
from typing import BinaryIO
from uuid import uuid4


_FILE_ID = re.compile(r"^file:[0-9a-f]{32}$")
_MOUNT_SUFFIX = ".mount"
_COPY_CHUNK = 1024 * 1024


class InvalidAttachmentName(ValueError):
    """上传边界的文件名不适用于本机文件系统。"""


def is_file_attachment_id(value: object) -> bool:
    return type(value) is str and _FILE_ID.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class FileAttachment:
    attachment_id: str
    name: str
    size: int


def _validate_name(name: str) -> None:
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


def _link_or_copy(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = dest.parent / f".staging-{uuid4().hex}"
    try:
        try:
            os.link(source, staging)
        except OSError:
            shutil.copyfile(source, staging)
        staging.replace(dest)
    except BaseException:
        if staging.exists():
            staging.unlink(missing_ok=True)
        raise


class FileAttachmentStore:
    def __init__(
        self, originals: Path, materials: Path,
        inherited_originals: tuple[Path, ...] = (),
    ):
        self.originals = originals
        self.materials = materials
        self._inherited_originals = inherited_originals

    def save_stream(self, stream: BinaryIO, name: str) -> FileAttachment:
        if not callable(getattr(stream, "read", None)):
            raise TypeError("file stream must be a binary IO")
        _validate_name(name)
        attachment_id, directory = self._create_original()
        target = directory / name
        staging = directory / f".staging-{uuid4().hex}"
        try:
            with staging.open("wb") as handle:
                shutil.copyfileobj(stream, handle, length=_COPY_CHUNK)
            staging.replace(target)
        except BaseException:
            if staging.exists():
                staging.unlink(missing_ok=True)
            raise
        return FileAttachment(attachment_id, name, target.stat().st_size)

    async def save_async(self, read, name: str) -> FileAttachment:
        if not callable(read):
            raise TypeError("file read must be callable")
        _validate_name(name)
        attachment_id, directory = self._create_original()
        target = directory / name
        staging = directory / f".staging-{uuid4().hex}"
        try:
            with staging.open("wb") as handle:
                while True:
                    chunk = await read(_COPY_CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
            staging.replace(target)
        except BaseException:
            if staging.exists():
                staging.unlink(missing_ok=True)
            raise
        return FileAttachment(attachment_id, name, target.stat().st_size)

    def save_path(self, source: Path) -> FileAttachment:
        if not isinstance(source, Path):
            raise TypeError("file source must be Path")
        resolved = source.resolve()
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        _validate_name(resolved.name)
        attachment_id, directory = self._create_original()
        self._mount_pointer(attachment_id).write_text(
            str(resolved), encoding="utf-8",
        )
        return FileAttachment(attachment_id, resolved.name, resolved.stat().st_size)

    def source(self, attachment_id: str) -> Path:
        if not is_file_attachment_id(attachment_id):
            raise ValueError("file attachment id 格式无效")
        for root in (self.originals, *self._inherited_originals):
            directory = root / attachment_id.removeprefix("file:")
            pointer = directory.with_name(directory.name + _MOUNT_SUFFIX)
            if pointer.is_file():
                text = pointer.read_text(encoding="utf-8")
                path = Path(text)
                if not text or not path.is_absolute():
                    raise ValueError("挂载指针必须是绝对路径")
                return path
            if directory.exists():
                entries = tuple(directory.iterdir())
                if len(entries) != 1 or not entries[0].is_file():
                    raise ValueError("附件原件目录必须且只能包含一个文件")
                return entries[0]
        raise FileNotFoundError(self._directory(attachment_id))

    def describe(self, attachment_id: str) -> FileAttachment:
        path = self.source(attachment_id)
        return FileAttachment(attachment_id, path.name, path.stat().st_size)

    def materialize(self, attachment_id: str) -> Path:
        source = self.source(attachment_id)
        directory = self.materials / attachment_id.removeprefix("file:")
        target = directory / source.name
        if not target.is_file():
            _link_or_copy(source, target)
        return target

    def _create_original(self) -> tuple[str, Path]:
        attachment_id = f"file:{uuid4().hex}"
        directory = self._directory(attachment_id)
        directory.mkdir(parents=True)
        return attachment_id, directory

    def _directory(self, attachment_id: str) -> Path:
        return self.originals / attachment_id.removeprefix("file:")

    def _mount_pointer(self, attachment_id: str) -> Path:
        directory = self._directory(attachment_id)
        return directory.with_name(directory.name + _MOUNT_SUFFIX)
