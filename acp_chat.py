from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from uuid import uuid4

from acp import run_agent

from redpanda.bootstrap import bootstrap_assistant
from redpanda.channels.acp import RedPandaAcpAgent


async def async_main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="工作区路径；缺省使用启动目录",
    )
    options = parser.parse_args(argv)
    agent: RedPandaAcpAgent | None = None

    def _push(coro) -> None:
        asyncio.get_running_loop().create_task(coro)

    async def sink(session_id: str, output_id: str, text: str) -> None:
        assert agent is not None
        await agent.deliver(session_id, output_id, text)

    async def preview(session_id: str, phase: str, output_id: str, text) -> None:
        assert agent is not None
        await agent.preview(session_id, phase, output_id, text)

    def report_tool(*values) -> None:
        assert agent is not None
        _push(agent.report_tool(*values))

    async def session_failed(session_id: str, message: str) -> None:
        await sink(session_id, f"session-failed-{uuid4().hex}", message)

    async with bootstrap_assistant(
        sink,
        workspace_path=Path.cwd() if options.workspace is None else options.workspace,
        tool_progress_sink=report_tool,
        preview_sink=preview,
        session_failed_sink=session_failed,
    ) as app:
        agent = RedPandaAcpAgent(app.sessions, app.workspaces)
        try:
            await run_agent(agent)
        finally:
            await agent.close()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
