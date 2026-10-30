"""The bundled image tool starts and restores through real stdio without a paid request."""
import asyncio

import pytest

from konden.config import write_json
from konden.llm.config import initial_connections
from konden.media.images import ImageConfig, ImageGeneration
from konden.mcp.bundled import IMAGE_SERVER_ID
from konden.mcp.composition import build_mcp
from konden.paths import KondenHome


pytestmark = pytest.mark.process


def test_image_tool_starts_restores_and_obeys_disable_and_read_only(tmp_path):
    home = KondenHome(tmp_path / "home")
    home.initialize()
    root = tmp_path / "work"
    root.mkdir()
    connections = initial_connections()
    connections["openai"]["api_key"] = "existing-private-key"
    write_json(home.connections_path, connections)
    service = ImageGeneration(home.connections_path)
    service.save(ImageConfig(enabled=True, model="openai/gpt-image-1.5"))
    before = home.connections_path.read_bytes()

    async def scenario():
        first = build_mcp(home, task_root=root)
        toolset_id = "mcp:" + IMAGE_SERVER_ID
        assert toolset_id not in {item.id for item in first.toolset_provider.descriptors(read_only_only=True)}
        async with first.client_manager:
            (loaded,) = await first.toolset_provider.discover_tools(toolset_id)
        second = build_mcp(home, task_root=root)
        async with second.client_manager:
            descriptor = next(item for item in second.toolset_provider.descriptors() if item.id == toolset_id)
            restored = second.toolset_provider.restore_spec(toolset_id=toolset_id, revision=descriptor.revision,
                name=loaded.spec.name, description=loaded.spec.description, parameters=loaded.spec.parameters.schema(),
                requires_authorization=loaded.spec.requires_authorization, provider_data=loaded.provider_data)
            result = await restored.handler({"prompt":" "})
            assert result["ok"] is False and "不能为空白" in result["error"]
            service.save(ImageConfig(model="openai/gpt-image-1.5"))
            result = await restored.handler({"prompt":"a blue square"})
            assert result["ok"] is False
            assert second.service.registry.snapshot() == ()
            assert not (home.mcp_root / "secrets").exists()
    asyncio.run(scenario())
    assert home.connections_path.read_bytes() == before
    assert not (root / "generated-images").exists()
