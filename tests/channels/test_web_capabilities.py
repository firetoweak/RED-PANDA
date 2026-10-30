from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from konden.channels.web.app import create_web_app
from konden.channels.web.capabilities import CapabilityViews
from konden.channels.web.channel import WebChannel
from konden.cli.models import CliHealth, CliRecord, CliSourceRef
from konden.cli.registry import CliRegistry
from konden.llm.config import initial_connections
from konden.mcp.composition import build_mcp
from konden.paths import KondenHome
from konden.skills.application import SkillApplicationService
from konden.skills.errors import SkillInstalledPackageError
from tests.skills.test_package import write_skill


@pytest.fixture
def capabilities(tmp_path):
    home = KondenHome(tmp_path / "home")
    home.initialize()
    skills = SkillApplicationService(home)
    mcp = build_mcp(home, client_manager=SimpleNamespace(invalidate=AsyncMock())).service
    views = CapabilityViews(skills, mcp, CliRegistry(home.clis_root))
    channel = WebChannel(SimpleNamespace(note_web_connected=Mock()), None)
    connection = channel.connect()
    source = tmp_path / "source"
    write_skill(source, name="review", description="独立检查交付成果")

    async def prepare():
        await skills.install_local(source)
        await skills.set_enabled("review", False)
        await mcp.upsert_server(
            server_id="reports", display_name="Reports", description="读取报告",
            transport="streamable_http", transport_config={"url": "https://example.invalid/mcp", "headers": {"Authorization": "Bearer secret-value"}},
        )
        await views.clis.add(CliRecord(
            name="git", description="版本控制", source=CliSourceRef("manifest", "git.json"),
            version="2.50.0", resolved_path="C:/tools/git.exe", health=CliHealth(True, True, False, datetime.now(timezone.utc)),
        ))

    asyncio.run(prepare())
    mcp.client_manager.invalidate.reset_mock()
    with TestClient(create_web_app(channel=channel, capabilities=views)) as client:
        yield client, views, {"connection_id": connection.connection_id}


def test_inventory_includes_disabled_and_builtin_capabilities_without_connecting_or_exposing_secrets(capabilities):
    """A hidden disabled installation or a credential leak would defeat the inventory's purpose."""
    client, views, _ = capabilities
    response = client.get("/api/capabilities")
    assert response.status_code == 200
    data = response.json()
    skill = next(item for item in data["skills"] if item["id"] == "review")
    assert not skill["enabled"]
    assert skill["origin"] == "installed"
    assert any(item["origin"] == "builtin" for item in data["skills"])
    server = next(item for item in data["mcp"] if item["id"] == "reports")
    assert (server["enabled"], server["last_status"]) == (False, "unknown")
    assert {item["id"] for item in data["mcp"] if item["origin"] == "builtin"} == {
        "builtin-playwright", "builtin-chrome-devtools", "builtin-deepseek", "builtin-openai-search",
        "builtin-image-generation",
    }
    assert not next(item for item in data["mcp"] if item["id"] == "builtin-deepseek")["enabled"]
    assert "secret-value" not in response.text
    assert "transport_config" not in response.text
    assert data["cli"][0]["version"] == "2.50.0"
    assert data["cli"][0]["health"]["help_ok"]
    settings = initial_connections()
    settings["deepseek"]["api_key"] = "private-deepseek-key"
    home = KondenHome(views.skills.skills_root.parent)
    home.connections_path.write_text(json.dumps(settings), encoding="utf-8")
    updated = client.get("/api/capabilities")
    deepseek = next(item for item in updated.json()["mcp"] if item["id"] == "builtin-deepseek")
    assert deepseek["enabled"] and deepseek["read_only"]
    assert "private-deepseek-key" not in updated.text
    views.mcp.client_manager.invalidate.assert_not_awaited()


def test_enable_requires_a_live_human_connection_and_changes_the_shared_registry(capabilities):
    """The UI must persist its switch, and an expired connection must not authorize a change."""
    client, views, connection = capabilities
    for kind, item_id in (("skills", "review"), ("mcp", "reports")):
        url = f"/api/capabilities/{kind}/{item_id}/enabled"
        assert client.put(url, json={"connection_id": "expired", "enabled": True}).status_code == 409
        assert client.put(url, json={**connection, "enabled": "true"}).status_code == 422
        assert client.put(url, json={**connection, "enabled": True}).json() == {"enabled": True}
        restored = (
            SkillApplicationService(KondenHome(views.skills.skills_root.parent)).registry
            if kind == "skills" else views.mcp.registry
        )
        assert asyncio.run(restored.get(item_id)).enabled
        inventory = client.get("/api/capabilities").json()
        assert next(item for item in inventory[kind] if item["id"] == item_id)["enabled"]
        assert client.put(url, json={**connection, "enabled": False}).json() == {"enabled": False}
    assert client.put("/api/capabilities/cli/git/enabled", json={**connection, "enabled": False}).status_code == 422
    assert client.put("/api/capabilities/skills/missing/enabled", json={**connection, "enabled": True}).status_code == 404
    assert client.put("/api/capabilities/mcp/builtin-playwright/enabled", json={**connection, "enabled": False}).status_code == 404


def test_enabling_a_skill_with_a_damaged_package_exposes_the_failure(capabilities):
    client, views, connection = capabilities
    (views.skills.skills_root / "packages/review/SKILL.md").write_text("damaged", encoding="utf-8")
    with pytest.raises(SkillInstalledPackageError):
        client.put("/api/capabilities/skills/review/enabled", json={**connection, "enabled": True})
    assert not asyncio.run(views.skills.registry.get("review")).enabled
