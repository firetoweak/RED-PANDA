"""Protect credential reuse, full-resolution delivery, and paid-request failure semantics."""
import asyncio
from base64 import b64encode
from io import BytesIO
from pathlib import Path
import json
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import httpx
from PIL import Image
from pydantic import ValidationError
import pytest

from konden.config import write_json
from konden.llm.config import initial_connections
from konden.media.images import ImageConfig, ImageGeneration, ImageInput
from konden.media.openai_images import GeneratedImage, ImageProviderError
from konden.mcp.image_generation import call_tool
from konden.model_settings import ModelConfigurationError


@pytest.fixture
def service(tmp_path):
    connections = initial_connections()
    connections["openai"]["api_key"] = "existing-private-key"
    path = tmp_path / "connections.json"
    write_json(path, connections)
    return ImageGeneration(path)


def png():
    buffer = BytesIO()
    Image.new("RGB", (3072, 2048), "navy").save(buffer, format="PNG")
    return buffer.getvalue()


def test_preferences_do_not_copy_keys_or_change_chat_and_search(service):
    chat = service.path.parent / "config.json"
    search = service.path.parent / "search.json"
    chat.write_text('{"model":{"default":null,"candidates":[]}}', encoding="utf-8")
    search.write_text('{"enabled":false,"provider":"deepseek","openai_model":null}', encoding="utf-8")
    before = [path.read_bytes() for path in (service.connections_path, chat, search)]
    assert service.config() == ImageConfig()
    service.save(ImageConfig(enabled=True, model="openai/gpt-image-1.5"))
    assert "private-key" not in service.path.read_text(encoding="utf-8")
    assert before == [path.read_bytes() for path in (service.connections_path, chat, search)]
    assert ImageGeneration(service.connections_path).config().enabled
    service.save(ImageConfig(model="openai/gpt-image-1.5"))
    assert service.config().model == "openai/gpt-image-1.5"


def test_generation_preserves_original_and_reads_updated_key_at_each_call(service, tmp_path):
    original = png()
    requests = []
    def handle(request):
        requests.append(request)
        assert request.url == "https://api.openai.com/v1/images/generations"
        body = json.loads(request.content)
        assert body == {"model":"gpt-image-1.5", "n":1, "prompt":"A blue square", "size":"auto",
                        "quality":"auto", "background":"auto", "output_format":"png"}
        return httpx.Response(200, json={"data":[{"b64_json":b64encode(original).decode()}]})
    transport = httpx.MockTransport(handle)
    service.save(ImageConfig(enabled=True, model="openai/gpt-image-1.5"))
    result = asyncio.run(service.generate(ImageInput(prompt="A blue square"), tmp_path, transport=transport))
    assert result.path.parent == tmp_path / "generated-images"
    assert result.path.read_bytes() == original
    assert (result.width, result.height) == (3072, 2048)
    with Image.open(BytesIO(result.preview)) as preview:
        assert preview.size == (1536, 1024)
    connections = initial_connections()
    connections["openai"]["api_key"] = "updated-private-key"
    write_json(service.connections_path, connections)
    second = asyncio.run(service.generate(ImageInput(prompt="A blue square"), tmp_path, transport=transport))
    assert second.path != result.path
    assert [request.headers["authorization"] for request in requests] == [
        "Bearer existing-private-key", "Bearer updated-private-key"]
    service.save(ImageConfig(model="openai/gpt-image-1.5"))
    with pytest.raises(ModelConfigurationError, match="停用"):
        asyncio.run(service.generate(ImageInput(prompt="A blue square"), tmp_path, transport=transport))
    assert len(requests) == 2


@pytest.mark.parametrize("failure", [401, 403, 429, 500, "timeout", "invalid-image"])
def test_provider_failures_never_retry_or_write_a_fake_image(service, tmp_path, failure):
    service.save(ImageConfig(enabled=True, model="openai/gpt-image-1.5"))
    requests = []
    def handle(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("existing-private-key", request=request)
        if failure == "invalid-image":
            return httpx.Response(200, json={"data":[{"b64_json":"aW52YWxpZA=="}]})
        return httpx.Response(failure, json={"error":{"message":"existing-private-key"}})
    with pytest.raises(ImageProviderError) as caught:
        asyncio.run(service.generate(ImageInput(prompt="blue"), tmp_path, transport=httpx.MockTransport(handle)))
    assert "existing-private-key" not in str(caught.value)
    assert len(requests) == 1
    assert not (tmp_path / "generated-images").exists()


def test_catalog_uses_actual_account_and_keeps_images_out_of_chat(service):
    def handle(request):
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data":[{"id":"gpt-4.1"}, {"id":"gpt-image-1.5"},
            {"id":"gpt-image-retired", "shutdown_date":"2020-01-01"}, {"id":"chatgpt-image-latest"}]})
    assert asyncio.run(service.catalog(transport=httpx.MockTransport(handle))) == [
        {"model":"openai/chatgpt-image-latest","name":"chatgpt-image-latest"},
        {"model":"openai/gpt-image-1.5","name":"gpt-image-1.5"}]


def test_tool_returns_a_relative_path_usable_by_send_file(service, tmp_path):
    service.save(ImageConfig(enabled=True, model="openai/gpt-image-1.5"))
    original = png()
    with patch("konden.media.images.generate_image", new=AsyncMock(
        return_value=GeneratedImage(original, "image/png", 3072, 2048))):
        result = asyncio.run(call_tool(service, tmp_path, None,
            SimpleNamespace(name="generate_image", arguments={"prompt":"blue square"})))
    relative = result.structured_content["path"]
    assert not Path(relative).is_absolute()
    assert (tmp_path / relative).read_bytes() == original
    assert relative.startswith("generated-images/")


def test_missing_key_and_invalid_parameters_cannot_initiate_generation(service):
    with pytest.raises(ValidationError):
        ImageInput(prompt=" ")
    with pytest.raises(ValidationError, match="透明背景"):
        ImageInput(prompt="blue", background="transparent", output_format="jpeg")
    with pytest.raises(ValidationError):
        ImageConfig(enabled=True)
    before = service.connections_path.read_bytes()
    write_json(service.connections_path, initial_connections())
    with pytest.raises(ModelConfigurationError, match="openai.api_key"):
        service.save(ImageConfig(enabled=True, model="openai/gpt-image-1.5"))
    assert not service.path.exists()
    service.connections_path.write_bytes(before)
    with patch("konden.media.images.generate_image", side_effect=LookupError("internal defect")):
        service.save(ImageConfig(enabled=True, model="openai/gpt-image-1.5"))
        with pytest.raises(LookupError, match="internal defect"):
            asyncio.run(service.generate(ImageInput(prompt="blue"), service.path.parent))
