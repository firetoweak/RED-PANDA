"""Image-generation preferences and delivery into the caller's working directory."""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Literal
from uuid import uuid4

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from konden.config import write_json
from konden.filesystem.workspace import EnvironmentPermissionDenied
from konden.llm.config import load_connections
from konden.media.openai_images import generate_image, list_image_models
from konden.model_settings import ModelConfigurationError
from thinllm import MissingProviderSetting, resolve_endpoint


class ImageConfig(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    enabled: bool = False
    model: str | None = None

    @field_validator("model")
    @classmethod
    def image_model(cls, value):
        if value is not None and (not value.startswith("openai/") or not value[7:]
                                  or any(character.isspace() for character in value)):
            raise ValueError("图片模型必须是 openai/<model>")
        return value

    @model_validator(mode="after")
    def enabled_model(self):
        if self.enabled and self.model is None:
            raise ValueError("请选择图片模型")
        return self


class ImageInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    prompt: str = Field(min_length=1, max_length=32000, description="要生成的图片内容、风格和构图。")
    size: str = Field(default="auto", pattern=r"^(auto|[1-9][0-9]{1,3}x[1-9][0-9]{1,3})$",
                      description="auto 或 宽x高（像素），例如 1536x1024；可用尺寸由所选模型决定。")
    quality: Literal["auto", "low", "medium", "high", "xhigh", "max"] = Field(
        default="auto", description="图片质量；xhigh 与 max 需要所选模型支持，质量越高通常耗时和费用越高。")
    background: Literal["auto", "opaque", "transparent"] = Field(default="auto", description="背景；透明背景使用 PNG 或 WebP。")
    output_format: Literal["png", "jpeg", "webp"] = Field(default="png", description="原图保存格式。")

    @field_validator("prompt")
    @classmethod
    def nonblank_prompt(cls, value):
        if not value.strip():
            raise ValueError("图片描述不能为空白")
        return value

    @model_validator(mode="after")
    def transparent_format(self):
        if self.background == "transparent" and self.output_format == "jpeg":
            raise ValueError("透明背景请选择 png 或 webp")
        return self


@dataclass(frozen=True)
class ImageDelivery:
    model: str
    path: Path
    mime: str
    width: int
    height: int
    preview: bytes


def image_config_path(connections_path: Path) -> Path:
    return connections_path.parent / "image-generation.json"


def load_image_config(path: Path) -> ImageConfig:
    return ImageConfig() if not path.exists() else ImageConfig.model_validate_json(path.read_bytes())


class ImageGeneration:
    def __init__(self, connections_path: Path):
        self.connections_path = connections_path
        self.path = image_config_path(connections_path)

    def config(self) -> ImageConfig:
        return load_image_config(self.path)

    def endpoint(self):
        connections = load_connections(self.connections_path)
        try:
            return resolve_endpoint("openai/images", connections["openai"])
        except MissingProviderSetting as error:
            raise ModelConfigurationError(f"请配置 openai.{error.setting}") from error

    async def catalog(self, *, transport=None) -> list[dict]:
        return await list_image_models(self.endpoint(), transport=transport)

    def save(self, config: ImageConfig) -> dict:
        if config.enabled:
            self.endpoint()
        write_json(self.path, config.model_dump())
        return config.model_dump()

    async def generate(self, arguments: ImageInput, output_root: Path, *, transport=None) -> ImageDelivery:
        config = self.config()
        if not config.enabled:
            raise ModelConfigurationError("图片生成已停用，请先在模型配置中启用。")
        root = output_root.resolve()
        directory = root / "generated-images"
        if not directory.resolve().is_relative_to(root):
            raise EnvironmentPermissionDenied(str(directory), "write")
        image = await generate_image(self.endpoint(), config.model[7:], arguments.model_dump(), transport=transport)
        directory.mkdir(exist_ok=True)
        target = directory / f"{uuid4().hex}.{arguments.output_format}"
        # New files only: never overwrite an existing work product.
        with target.open("xb") as handle:
            handle.write(image.data)
        with Image.open(BytesIO(image.data)) as preview:
            preview.thumbnail((1536, 1536))
            buffer = BytesIO()
            preview.save(buffer, format="PNG")
        return ImageDelivery(config.model, target, image.mime, image.width, image.height, buffer.getvalue())
