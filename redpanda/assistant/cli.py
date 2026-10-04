from __future__ import annotations

from collections.abc import Mapping

from redpanda.assistant.artifacts import ArtifactGateway
from redpanda.assistant.context.projection import (
    ModelContextSettings,
    externalize_tool_result,
)
from redpanda.runtime import ToolBinding
from redpanda.runtime.dispatcher import AttemptContext
from redpanda.assistant.tool_results import runtime_tool_result
from redpanda.tools.spec import ToolArgumentsError
from redpanda.cli.runtime import LOAD_CLI
from redpanda.assistant.catalog import CatalogCli


class CliToolAdapter:
    """把已登记 CLI 投影为一个普通 Runtime 工具 load_cli。"""

    def __init__(
        self,
        cli,
        gateway: ArtifactGateway,
        settings: ModelContextSettings,
    ) -> None:
        self.cli = cli
        self._catalog = cli.tool_catalog
        self._gateway = gateway
        self._settings = settings
        self._catalogs: dict[str, dict[str, int]] = {}

    def registry_catalog(self) -> list[dict[str, object]]:
        self._catalog.tool_specs()
        return sorted(
            (
                {
                    "id": record.name,
                    "description": record.description,
                    "version": record.version,
                    "revision": record.revision,
                }
                for record in self._catalog.registry.snapshot()
            ),
            key=lambda item: item["id"],
        )

    def apply_catalog(
        self,
        session_id: str,
        clis: tuple[CatalogCli, ...],
    ) -> None:
        self._catalogs[session_id] = {
            item.id: item.revision for item in clis
        }

    def schemas(self) -> list[dict[str, object]]:
        return [spec.to_openai_tool() for spec in self._catalog.tool_specs({})]

    def bindings(self) -> dict[str, ToolBinding]:
        return {LOAD_CLI: ToolBinding(self._handler(LOAD_CLI))}

    def _handler(self, name: str):
        async def handler(
            context: AttemptContext,
            arguments: Mapping[str, object],
        ) -> object:
            specs = {
                spec.name: spec
                for spec in self._catalog.tool_specs(
                    self._catalogs[context.session_id]
                )
            }
            spec = specs.get(name)
            if spec is None:
                return {
                    "ok": False,
                    "code": "CLI_NOT_AVAILABLE",
                    "error": "当前没有已登记的 CLI",
                    "hint": "使用 /cli list 查看已登记 CLI。",
                }
            try:
                payload = spec.parameters.validate(dict(arguments))
            except ToolArgumentsError as exc:
                return {
                    "ok": False,
                    "code": "VALIDATION_ERROR",
                    "data": {"details": exc.details},
                    "error": "cli arguments validation failed",
                }
            result = runtime_tool_result(await spec.handler(payload))
            return externalize_tool_result(
                result,
                context.session_id,
                self._gateway,
                self._settings,
            )

        return handler
