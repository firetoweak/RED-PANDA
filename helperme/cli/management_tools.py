from __future__ import annotations

from pydantic import BaseModel

from helperme.cli.application import CliApplicationService
from helperme.cli.errors import CliInputError, CliNotFoundError
from helperme.tools.spec import EmptyInput, PydanticParameters, ToolSpec


LIST_INSTALLED_CLIS = "list_installed_clis"
INSPECT_CLI = "inspect_cli"
TEST_CLI = "test_cli"


class CliIdInput(BaseModel):
    cli_id: str


def create_cli_management_specs(
    service: CliApplicationService,
) -> tuple[ToolSpec, ...]:
    async def list_clis(_input: EmptyInput):
        records = await service.list_clis()
        return {
            "ok": True,
            "code": "CLIS_LISTED",
            "data": {
                "clis": [record.to_dict() for record in records],
            },
        }

    async def inspect(input_data: CliIdInput):
        try:
            record = await service.inspect(input_data.cli_id)
        except CliNotFoundError:
            return _not_found(input_data.cli_id)
        return {
            "ok": True,
            "code": "CLI_INSPECTED",
            "data": {"record": record.to_dict()},
        }

    async def test_cli(input_data: CliIdInput):
        try:
            result = await service.test_cli(input_data.cli_id)
        except CliNotFoundError as exc:
            return _not_found(input_data.cli_id, detail=str(exc))
        except CliInputError as exc:
            return _invalid(input_data.cli_id, exc)
        return {
            "ok": True,
            "code": "CLI_TEST_PASSED",
            "data": {
                "cli_id": result.record.name,
                "revision": result.record.revision,
                "version": result.probed.version,
                "health": result.probed.health.to_dict(),
            },
        }

    return (
        ToolSpec(
            LIST_INSTALLED_CLIS,
            "列出已登记 CLI 的管理目录。",
            PydanticParameters(EmptyInput),
            list_clis,
        ),
        ToolSpec(
            INSPECT_CLI,
            "检查已登记 CLI 的登记信息与体检详情。",
            PydanticParameters(CliIdInput),
            inspect,
        ),
        ToolSpec(
            TEST_CLI,
            "重跑已登记 CLI 的体检（--help / --version），返回测得的最新事实；"
            "只做诊断，不更新已登记的信息。",
            PydanticParameters(CliIdInput),
            test_cli,
        ),
    )


def _not_found(cli_id: str, *, detail: str | None = None) -> dict:
    return {
        "ok": False,
        "code": "CLI_NOT_INSTALLED",
        "data": {"cli_id": cli_id},
        "error": (
            f"CLI 未登记: {cli_id}" if detail is None else detail
        ),
        "hint": "先调用 list_installed_clis 查看管理目录。",
    }


def _invalid(cli_id: str, exc: CliInputError) -> dict:
    return {
        "ok": False,
        "code": "CLI_INVALID",
        "data": {"cli_id": cli_id},
        "error": str(exc),
        "hint": "检查已登记 CLI 与系统实际状态后重试。",
    }
