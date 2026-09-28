"""Session 附件：图片按内容寻址，普通上传文件保留独立身份与原始名称。

文本 Artifact 因超长而外置、按字符分页；附件因二进制天然写不进 Journal 而外置、
整件取回。两者共用 Session 目录，不共用 API。见 docs/架构/上下文/多模态附件.md。
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import re
from uuid import uuid4

from PIL import Image

from helperme.assistant.asset_locations import inherited_drawers
from helperme.assistant.file_attachments import (
    FileAttachment, FileAttachmentStore, InvalidAttachmentName, is_file_attachment_id,
)

from helperme.runtime import ToolBinding
from helperme.runtime.events import CommandOutcomeReceived
from helperme.runtime.json_values import thaw_value


MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 64_000_000
NORMALIZED_MAX_DIMENSION = 2048

_MIME_BY_FORMAT = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}

_ATTACHMENT_ID_PREFIX = "sha256:"
_ATTACHMENT_ID_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class AttachmentRejected(ValueError):
    """外部边界拒绝：格式不在白名单、体积超限，或声明与实际不符。"""


def is_valid_attachment_id(value: object) -> bool:
    return is_image_attachment_id(value) or is_file_attachment_id(value)


def is_image_attachment_id(value: object) -> bool:
    return type(value) is str and _ATTACHMENT_ID_PATTERN.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class AttachmentRef:
    attachment_id: str
    mime: str
    width: int
    height: int
    source_width: int
    source_height: int

    def to_block(self) -> dict[str, object]:
        """模型侧引用块；Client 在请求边界据此读字节。"""

        return {
            "type": "image",
            "id": self.attachment_id,
            "mime": self.mime,
            "width": self.width,
            "height": self.height,
            "source_width": self.source_width,
            "source_height": self.source_height,
        }


def _admit(data: bytes, declared_mime: str) -> tuple[bytes, str, tuple[int, int], tuple[int, int]]:
    if len(data) > MAX_SOURCE_BYTES:
        raise AttachmentRejected(
            f"图片字节数 {len(data)} 超过上限 {MAX_SOURCE_BYTES}"
        )
    try:
        image = Image.open(BytesIO(data))
    except OSError as exc:
        raise AttachmentRejected(f"无法识别图片: {exc}") from exc
    with image:
        mime = _MIME_BY_FORMAT.get(image.format)
        if mime is None:
            raise AttachmentRejected(f"不支持的图片格式: {image.format}")
        if mime != declared_mime:
            raise AttachmentRejected(
                f"声明 MIME {declared_mime} 与实际格式 {mime} 不符"
            )
        source_size = image.size
        if source_size[0] * source_size[1] > MAX_PIXELS:
            raise AttachmentRejected(
                f"图片像素数 {source_size[0] * source_size[1]} 超过上限 {MAX_PIXELS}"
            )
        try:
            image.load()
        except OSError as exc:
            raise AttachmentRejected(f"图片数据损坏: {exc}") from exc
        longest = max(source_size)
        if longest <= NORMALIZED_MAX_DIMENSION:
            return data, mime, source_size, source_size
        scale = NORMALIZED_MAX_DIMENSION / longest
        size = (
            max(1, round(source_size[0] * scale)),
            max(1, round(source_size[1] * scale)),
        )
        buffer = BytesIO()
        image.resize(size, Image.LANCZOS).save(buffer, format=image.format)
        return buffer.getvalue(), mime, size, source_size


class AttachmentStore:
    """单个 Session 的附件抽屉。"""

    def __init__(self, root: Path, inherited: tuple[Path, ...] = ()) -> None:
        self._root = root.resolve()
        self._inherited = inherited
        self.files = FileAttachmentStore(
            self._root / "files", self._root.parent / ".materials",
            tuple(path / "files" for path in inherited),
        )

    def save_file_stream(self, stream, name: str) -> FileAttachment:
        try:
            return self.files.save_stream(stream, name)
        except InvalidAttachmentName as error:
            raise AttachmentRejected(str(error)) from error

    async def save_file_upload(self, read, name: str) -> FileAttachment:
        try:
            return await self.files.save_async(read, name)
        except InvalidAttachmentName as error:
            raise AttachmentRejected(str(error)) from error

    def save_file_path(self, source: Path) -> FileAttachment:
        try:
            return self.files.save_path(source)
        except (InvalidAttachmentName, FileNotFoundError) as error:
            raise AttachmentRejected(
                "只能挂入本机已存在的文件" if isinstance(error, FileNotFoundError)
                else str(error)
            ) from error

    def save_image(self, data: bytes, declared_mime: str) -> AttachmentRef:
        stored, mime, size, source_size = _admit(data, declared_mime)
        digest = sha256(stored).hexdigest()
        path = self._root / digest
        if not path.is_file():
            self._root.mkdir(parents=True, exist_ok=True)
            staging = self._root / f".staging-{uuid4().hex}"
            staging.write_bytes(stored)
            staging.replace(path)
        return AttachmentRef(
            f"{_ATTACHMENT_ID_PREFIX}{digest}",
            mime,
            size[0],
            size[1],
            source_size[0],
            source_size[1],
        )

    def path(self, attachment_id: str) -> Path:
        if is_file_attachment_id(attachment_id):
            return self.files.source(attachment_id)
        if not is_valid_attachment_id(attachment_id):
            raise ValueError("attachment id 格式无效")
        name = attachment_id[len(_ATTACHMENT_ID_PREFIX) :]
        own = self._root / name
        return next(
            (path for path in (own, *(root / name for root in self._inherited))
             if path.is_file()),
            own,
        )

    def read(self, attachment_id: str) -> bytes:
        # 附件缺失是持久化损坏，原样抛出，不降级成「没图」。
        return self.path(attachment_id).read_bytes()

    def inspect(self, attachment_id: str) -> AttachmentRef:
        """从已落盘字节还原引用。用户消息的 artifact_refs 只有 id。"""

        data = self.read(attachment_id)
        with Image.open(BytesIO(data)) as image:
            mime = _MIME_BY_FORMAT[image.format]
            width, height = image.size
        return AttachmentRef(attachment_id, mime, width, height, width, height)


class AttachmentGateway:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._inherited: dict[str, tuple[Path, ...]] = {}

    def for_session(self, session_id: str) -> AttachmentStore:
        drawer = sha256(session_id.encode("utf-8")).hexdigest()
        inherited = self._inherited.get(session_id)
        if inherited is None:
            inherited = inherited_drawers(self._root, session_id, ".attachments")
            self._inherited[session_id] = inherited
        return AttachmentStore(self._root / drawer / ".attachments", inherited)


READ_IMAGE_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "read_image",
        "description": (
            "重新查看本对话中用户或工具曾提供的图片。"
            "只能使用事实里真实出现过的附件 id；"
            "压缩后的文字引用不代表当前仍看得到图片。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "pattern": r"^sha256:[0-9a-f]{64}$",
                },
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    },
}


def read_image_binding(journal, store: AttachmentStore) -> dict[str, ToolBinding]:
    async def handler(context, arguments):
        attachment_id = arguments.get("id")
        if set(arguments) != {"id"} or not is_valid_attachment_id(attachment_id):
            return {
                "ok": False,
                "code": "INVALID_ARGUMENT",
                "error": "需要本对话中出现过的附件 id",
            }
        if not is_image_attachment_id(attachment_id):
            return {"ok": False, "code": "NOT_AN_IMAGE", "error": "该附件是普通文件，请使用消息中的材料路径读取"}
        for event in await journal.snapshot(context.session_id):
            if attachment_id in event.artifact_refs:
                return {
                    "ok": True,
                    "code": "IMAGE_READ",
                    "data": {"id": attachment_id},
                    "images": [store.inspect(attachment_id).to_block()],
                }
            payload = event.payload
            if (
                not isinstance(payload, CommandOutcomeReceived)
                or payload.outcome.value is None
            ):
                continue
            value = thaw_value(payload.outcome.value)
            if type(value) is not dict:
                continue
            for image in value.get("images") or []:
                if image["id"] == attachment_id:
                    store.read(attachment_id)
                    return {
                        "ok": True,
                        "code": "IMAGE_READ",
                        "data": {"id": attachment_id},
                        "images": [image],
                    }
        return {
            "ok": False,
            "code": "IMAGE_NOT_IN_SESSION",
            "error": "图片不属于当前 Session",
        }

    return {"read_image": ToolBinding(handler)}
