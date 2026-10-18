"""每次启动都清空会话的入口不能删掉长期 Agent 的工作树。"""

from __future__ import annotations

import asyncio
import re

import pytest

from redpanda import bootstrap
from redpanda.paths import RedPandaHome


def test_fresh_entry_refuses_a_data_root_with_agents(tmp_path, monkeypatch):
    home = RedPandaHome(tmp_path / "home")
    worktree = home.agent_worktrees_root / "project" / "agent"
    worktree.mkdir(parents=True)
    monkeypatch.setenv("REDPANDA_HOME", str(home.root))
    monkeypatch.setattr(bootstrap, "install_host_environment", lambda: None)
    monkeypatch.setattr(bootstrap, "install_timing_logging", lambda: None)

    async def start():
        async with bootstrap.bootstrap_assistant(lambda *_: None, fresh_sessions=True):
            pass

    with pytest.raises(bootstrap.DataRootHasAgents, match=re.escape(str(home.root))):
        asyncio.run(start())
    assert worktree.is_dir()
