from __future__ import annotations

import asyncio
import os
import unittest
from pathlib import Path

import pytest

from helperme.runtime import MemoryJournal, RuntimeStatus
from tests.session_scheduler import build_settling_assistant as build_live_assistant
from helperme.config import assistant_config_from_app, load_app_config
from helperme.llm.config import load_gateway_config
from helperme.llm.ferro_client import FerroClient
from helperme.paths import HelperMeHome
from helperme.sandbox.registry import WorkspaceRegistry

pytestmark = pytest.mark.live


@unittest.skipUnless(
    os.environ.get("HELPERME_RUN_LIVE_TESTS") == "1",
    "设置 HELPERME_RUN_LIVE_TESTS=1 后显式运行 live 测试",
)
class RuntimeLiveModelTest(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_step_calls_real_model_and_delivers(self):
        app_config = load_app_config()
        config = assistant_config_from_app(
            app_config,
            FerroClient(load_gateway_config()),
        )
        delivered: list[str] = []
        journal = MemoryJournal()
        session_id = "live-session"
        # live 会话跑在当前目录：登记进本机 registry（幂等），
        # supervisor 建会话时按归属校验。
        workspace = WorkspaceRegistry.load(
            HelperMeHome.default().workspaces_path
        ).register_path(Path.cwd())
        assembly = await build_live_assistant(
            config, delivered.append, journal, session_id, workspace
        )
        try:
            async with config.llm, assembly.mcp.client_manager:
                await assembly.sessions.create(session_id, workspace.workspace_id)
                await assembly.sessions.receive_user_message(
                    session_id,
                    "只用一句话回答：1+1 等于几。不要调用工具。",
                    delivery_id="live-1",
                )
                await asyncio.wait_for(
                    assembly.scheduler.join(),
                    timeout=180,
                )
                state = await assembly.runtime.state(session_id)
        finally:
            await assembly.scheduler.close()
        events = await journal.snapshot(session_id)
        kinds = [event.payload.__class__.__name__ for event in events]
        self.assertIn("UserMessageReceived", kinds)
        self.assertIn("StepCommitted", kinds)
        self.assertTrue(delivered, kinds)
        self.assertEqual(state.status, RuntimeStatus.WAITING)
        self.assertEqual(state.waiting_for, ("external_fact",))
