"""OpenAI Images HTTP boundary; no tools, preferences, or file ownership."""
from __future__ import annotations

from base64 import b64decode
from binascii import Error as Base64Error
from dataclasses import dataclass
from datetime import date
from io import BytesIO

import httpx
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from thinllm import Endpoint


class ImageProviderError(Exception):
    """A known failure at the external image-service boundary."""


@dataclass(frozen=True)
class GeneratedImage:
    data: bytes
    mime: str
    width: int
    height: int


class _Model(BaseModel):
    model_config = ConfigDict(strict=True)
    id: str = Field(min_length=1)
    shutdown_date: date | None = None


class _Catalog(BaseModel):
    model_config = ConfigDict(strict=True)
    data: list[_Model]


class _Image(BaseModel):
    model_config = ConfigDict(strict=True)
    b64_json: str = Field(min_length=1)


class _Generation(BaseModel):
    model_config = ConfigDict(strict=True)
    data: list[_Image] = Field(min_length=1, max_length=1)


async def _request(endpoint: Endpoint, method: str, path: str, *, body=None, transport=None):
    try:
        async with httpx.AsyncClient(
            base_url=endpoint.base_url + "/", headers={"Authorization": f"Bearer {endpoint.api_key}"},
            timeout=httpx.Timeout(180, connect=15), transport=transport,
        ) as client:
            response = await client.request(method, path, json=body)
    except httpx.TransportError as error:
        detail = ("请求中断，无法确认是否已生成图片；请核对用量后再决定是否重新生成。"
                  if method == "POST" else "图片模型目录连接失败，请重试。")
        raise ImageProviderError(detail) from error
    if response.is_error:
        if response.status_code in (401, 403):
            detail = "认证或访问失败，请检查 OpenAI 连接与图片模型权限。"
        elif response.status_code == 429:
            detail = "请求受限，请检查 OpenAI 额度与速率限制。"
        else:
            detail = "请求失败，请检查模型、生成参数和服务状态。"
        raise ImageProviderError(f"OpenAI 图片服务 HTTP {response.status_code}：{detail}")
    return response


async def list_image_models(endpoint: Endpoint, *, transport=None) -> list[dict]:
    response = await _request(endpoint, "GET", "models", transport=transport)
    try:
        catalog = _Catalog.model_validate_json(response.content)
    except ValidationError as error:
        raise ImageProviderError("OpenAI 图片模型目录格式不符合约定。") from error
    return sorted(
        ({"model": "openai/" + item.id, "name": item.id} for item in catalog.data
         if item.id.startswith(("gpt-image-", "chatgpt-image-"))
         and (item.shutdown_date is None or item.shutdown_date > date.today())),
        key=lambda item: item["model"],
    )


async def generate_image(endpoint: Endpoint, model: str, parameters: dict, *, transport=None) -> GeneratedImage:
    response = await _request(endpoint, "POST", "images/generations",
                              body={"model": model, "n": 1, **parameters}, transport=transport)
    try:
        result = _Generation.model_validate_json(response.content)
        data = b64decode(result.data[0].b64_json, validate=True)
        with Image.open(BytesIO(data)) as image:
            expected = {"png": "PNG", "jpeg": "JPEG", "webp": "WEBP"}[parameters["output_format"]]
            if image.format != expected:
                raise ImageProviderError("OpenAI 返回的图片格式与请求不符。")
            image.load()
            width, height = image.size
    except (ValidationError, Base64Error, ValueError, UnidentifiedImageError, OSError,
            Image.DecompressionBombError) as error:
        raise ImageProviderError("OpenAI 未返回完整有效的图片；请核对用量后再决定是否重新生成。") from error
    return GeneratedImage(data, "image/" + parameters["output_format"], width, height)
