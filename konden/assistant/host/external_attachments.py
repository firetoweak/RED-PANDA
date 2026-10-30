"""Attachment snapshots and descriptions for the fixed three-party conversations."""
from __future__ import annotations

from pathlib import Path

from konden.assistant.attachments import AttachmentRejected, MAX_SOURCE_BYTES, is_file_attachment_id


IMAGE_MIMES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".webp": "image/webp", ".gif": "image/gif"}


def snapshot_files(store, root: Path, paths: list[str]) -> list[str]:
    root = root.resolve()
    sources = []
    for relative in paths:
        path = Path(relative)
        if not relative.strip() or path.is_absolute() or ".." in path.parts:
            raise AttachmentRejected("附件必须使用当前聊天工作目录内的相对路径。")
        source = (root / path).resolve()
        if not source.is_relative_to(root) or not source.is_file():
            raise AttachmentRejected("聊天工作目录里没有这个附件，或文件位于目录之外。")
        sources.append(source)
    refs = []
    for source in sources:
        try:
            with source.open("rb") as stream:
                mime = IMAGE_MIMES.get(source.suffix.lower())
                ref = (store.save_image(stream.read(MAX_SOURCE_BYTES + 1), mime) if mime is not None
                       else store.save_file_stream(stream, source.name))
        except (FileNotFoundError, PermissionError) as error:
            raise AttachmentRejected("这个附件不存在或无法读取。") from error
        refs.append(ref.attachment_id)
    return refs


def describe_attachments(store, refs: list[str]) -> list[dict]:
    result = []
    for ref in refs:
        if is_file_attachment_id(ref):
            file = store.files.describe(ref)
            result.append({"kind": "file", "attachment_id": ref, "name": file.name, "size": file.size})
        else:
            image = store.inspect(ref)
            result.append({"kind": "image", "attachment_id": ref, "name": "图片", "size": store.path(ref).stat().st_size,
                           "mime": image.mime, "width": image.width, "height": image.height})
    return result
