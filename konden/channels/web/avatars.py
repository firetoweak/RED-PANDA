"""Web-only avatar choices and image bytes, separate from conversation attachments."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from konden.assistant.attachments import AttachmentRejected, AttachmentStore, is_image_attachment_id
from konden.config import write_json


class GeneratedAvatar(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    kind: Literal["generated"]
    seed: str = Field(min_length=1)


class ImageAvatar(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    kind: Literal["image"]
    attachment_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


AvatarSelection = GeneratedAvatar | ImageAvatar
_SELECTIONS = TypeAdapter(dict[str, AvatarSelection])


class AvatarImageNotFound(LookupError):
    """The requested image is absent from Web avatar storage."""


class AvatarStore:
    def __init__(self, root: Path):
        self._path = root / "avatars.json"
        self._images = AttachmentStore(root / "images")

    def read(self) -> dict[str, AvatarSelection]:
        if not self._path.exists():
            return {}
        return _SELECTIONS.validate_json(self._path.read_text(encoding="utf-8"))

    def set_generated(self, session_id: str, seed: str | None) -> GeneratedAvatar | None:
        selection = None if seed is None else GeneratedAvatar(kind="generated", seed=seed)
        self._save(session_id, selection)
        return selection

    def save_image(self, session_id: str, data: bytes, mime: str) -> ImageAvatar:
        image = self._images.save_image(data, mime)
        selection = ImageAvatar(kind="image", attachment_id=image.attachment_id)
        self._save(session_id, selection)
        return selection

    def image_file(self, attachment_id: str) -> tuple[Path, str]:
        if not is_image_attachment_id(attachment_id):
            raise AttachmentRejected("头像图片 id 格式无效")
        if not self._images.path(attachment_id).is_file():
            raise AvatarImageNotFound(attachment_id)
        return self._images.path(attachment_id), self._images.inspect(attachment_id).mime

    def _save(self, session_id: str, selection: AvatarSelection | None) -> None:
        selections = self.read()
        if selection is None:
            selections.pop(session_id, None)
        else:
            selections[session_id] = selection
        write_json(self._path, {key: value.model_dump() for key, value in selections.items()})
