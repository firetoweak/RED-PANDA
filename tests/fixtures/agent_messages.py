"""A real Worker exits at either side of its private-message side effect."""
from __future__ import annotations

import os

from redpanda.assistant.agent_messages import SEND_MESSAGE
from redpanda.runtime import ToolBinding
from tests.fixtures.session_worker import direct_workspace_config


def interrupted_send_config(root, when):
    from redpanda.assistant.host import worker

    build = worker.build_assistant_assembly

    async def assembly(*args, **kwargs):
        built = await build(*args, **kwargs)
        if SEND_MESSAGE in built.bindings:
            original = built.bindings[SEND_MESSAGE]

            async def interrupted(context, arguments):
                marker = root / f"send-crashed-{when}"
                if not marker.exists() and when == "before":
                    marker.touch()
                    os._exit(1)
                result = await original.handler(context, arguments)
                if not marker.exists() and when == "after":
                    marker.touch()
                    os._exit(1)
                return result

            built.runtime.dispatcher.bind(SEND_MESSAGE, ToolBinding(interrupted))
        return built

    worker.build_assistant_assembly = assembly
    return direct_workspace_config(root)
