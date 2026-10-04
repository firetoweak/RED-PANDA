from __future__ import annotations

from pathlib import Path

from redpanda.skills.application import SkillApplicationService
from redpanda.skills.models import SkillSourceRef
from redpanda.skills.sources import SkillSourceError


class SkillCommandError(ValueError):
    pass


class SkillConsoleAdapter:
    def __init__(self, service: SkillApplicationService) -> None:
        self._service = service

    async def execute_if_handled(self, user_message: str) -> str | None:
        if not user_message.startswith("/skill"):
            return None
        parts = user_message.split(maxsplit=2)
        if len(parts) == 1 or parts[1] == "help":
            return self._help()
        action = parts[1]
        rest = parts[2] if len(parts) > 2 else ""
        if action == "list":
            return await self._list(rest)
        if action == "install":
            return await self._install(rest)
        if action == "test":
            return await self._test(rest)
        if action == "set_enabled":
            args = rest.split()
            if len(args) != 2 or args[1] not in {"true", "false"}:
                raise SkillCommandError("/skill set_enabled <id> <true|false>")
            return self._with_next_step(await self._set_enabled(args[0], args[1] == "true"))
        if action == "uninstall":
            return self._with_next_step(await self._remove(rest))
        if action == "update":
            return self._with_next_step(await self._update(rest))
        raise SkillCommandError(f"未知 /skill 子命令: {action}")

    async def _list(self, rest: str) -> str:
        records = await self._service.list_skills()
        if rest.strip():
            records = tuple(item for item in records if item.name == rest.strip())
        if not records:
            return "尚未安装任何 Skill。"
        return "\n".join(
            f"- {item.name} [{'enabled' if item.enabled else 'disabled'}] "
            f"{item.description} (revision={item.revision})"
            for item in records
        )

    async def _install(self, rest: str) -> str:
        source = rest.strip()
        if not source:
            raise SkillCommandError(
                "/skill install [local|url|github] <locator> [ref]"
            )
        parts = source.split(maxsplit=2)
        if parts[0] in {"local", "url", "github"}:
            if len(parts) < 2:
                raise SkillCommandError(
                    "/skill install [local|url|github] <locator> [ref]"
                )
            kind = parts[0]
            locator = parts[1]
            requested_ref = parts[2] if len(parts) == 3 else None
            source_ref = SkillSourceRef(kind, locator, requested_ref)
        else:
            source_ref = SkillSourceRef("local", str(Path(source).resolve()))
        try:
            record = await self._service.install_source(source_ref)
        except SkillSourceError as exc:
            raise SkillCommandError(str(exc)) from exc
        return (
            f"已安装并启用 Skill `{record.name}`，包完整性校验通过。"
            "\n下一个 Step 进入能力目录，正文按需加载。"
        )

    async def _test(self, rest: str) -> str:
        skill_id = self._required_id(rest, "test")
        inspection = await self._service.test_skill(skill_id)
        return (
            f"Skill `{skill_id}` 校验通过："
            f"{len(inspection.files)} files, "
            f"{inspection.main_instruction_chars} instruction chars"
        )

    async def _set_enabled(self, rest: str, enabled: bool) -> str:
        action = "set_enabled"
        skill_id = self._required_id(rest, action)
        record = await self._service.set_enabled(skill_id, enabled)
        state = "启用" if enabled else "停用"
        return f"已{state} Skill `{record.name}` (revision={record.revision})"

    async def _remove(self, rest: str) -> str:
        skill_id = self._required_id(rest, "uninstall")
        record = await self._service.remove(skill_id)
        return f"已卸载 Skill `{record.name}`"

    async def _update(self, rest: str) -> str:
        parts = rest.split(maxsplit=3)
        if not parts:
            raise SkillCommandError(
                "/skill update <id> [[local|url|github] <locator> [ref]]"
            )
        skill_id = parts[0]
        replacement = None
        if len(parts) > 1:
            if len(parts) < 3 or parts[1] not in {"local", "url", "github"}:
                raise SkillCommandError(
                    "/skill update <id> [[local|url|github] <locator> [ref]]"
                )
            replacement = SkillSourceRef(
                parts[1],
                parts[2],
                parts[3] if len(parts) == 4 else None,
            )
        report = await self._service.check_update(skill_id, replacement)
        if not report.candidate.diff.changed:
            return f"Skill `{skill_id}` 内容没有变化。"
        record = await self._service.update(skill_id, report.candidate.candidate_hash)
        return f"已更新 Skill `{record.name}` (revision={record.revision})"

    @staticmethod
    def _required_id(rest: str, action: str) -> str:
        skill_id = rest.strip()
        if not skill_id:
            raise SkillCommandError(f"/skill {action} <id>")
        return skill_id

    @staticmethod
    def _with_next_step(message: str) -> str:
        return f"{message}\n能力集合变化从下一个 Step 生效。"

    @staticmethod
    def _help() -> str:
        return (
            "Skill 包完整安装到 Agent HOME，安装默认启用；正文按需加载。\n"
            "  /skill list [id]\n"
            "  /skill install [local|url|github] <locator> [ref]\n"
            "  /skill test <id>\n"
            "  /skill set_enabled <id> <true|false>\n"
            "  /skill uninstall <id>\n"
            "  /skill update <id> [[local|url|github] <locator> [ref]]"
        )
