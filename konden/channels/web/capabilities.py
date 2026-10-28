"""Web views of the three independent capability domains; no discovery or execution."""

from __future__ import annotations

from dataclasses import dataclass

from konden.cli.registry import CliRegistry
from konden.mcp.application import McpApplicationService
from konden.mcp.models import McpServerRecord
from konden.skills.application import SkillApplicationService


@dataclass(frozen=True)
class CapabilityViews:
    skills: SkillApplicationService
    mcp: McpApplicationService
    clis: CliRegistry

    async def read(self) -> dict:
        skills = [
            {
                "id": item.id,
                "name": item.id.removeprefix("builtin:"),
                "description": item.description,
                "origin": "builtin",
                "enabled": True,
                "source": "随 konden 提供",
                "version": None,
                "path": str(item.directory),
            }
            for item in self.skills.tool_catalog.bundled_skills
        ]
        skills.extend(
            {
                "id": record.name,
                "name": record.name,
                "description": record.description,
                "origin": "installed",
                "enabled": record.enabled,
                "source": record.source.locator,
                "version": record.resolved_ref,
                "path": str(self.skills.skills_root / "packages" / record.name),
            }
            for record in await self.skills.list_skills()
        )
        servers = [_mcp_view(record, "builtin") for record in self.mcp.bundled_servers()]
        servers.extend(
            _mcp_view(item.record, "installed")
            for item in await self.mcp.list_servers()
        )
        clis = [
            {
                "id": record.name,
                "name": record.name,
                "description": record.description,
                "source": record.source.locator,
                "version": record.version,
                "path": record.resolved_path,
                "health": None if record.health is None else record.health.to_dict(),
            }
            for record in await self.clis.list_clis()
        ]
        return {
            "skills": sorted(skills, key=lambda item: item["name"].casefold()),
            "mcp": sorted(servers, key=lambda item: item["name"].casefold()),
            "cli": clis,
        }


def _mcp_view(record: McpServerRecord, origin: str) -> dict:
    # Transport configuration can contain credentials and never belongs in this view.
    return {
        "id": record.id,
        "name": record.display_name,
        "description": record.description,
        "origin": origin,
        "enabled": record.enabled,
        "transport": record.transport.value,
        "read_only": record.read_only,
        "last_status": record.last_status.value,
        "last_checked_at": None if record.last_checked_at is None else record.last_checked_at.isoformat(),
        "last_error_summary": record.last_error_summary or None,
    }
