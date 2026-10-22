"""每次启动都清空会话的入口不能删掉长期 Bot 的数据。"""

from __future__ import annotations

import asyncio
import re

import pytest

from redpanda import bootstrap
from redpanda.paths import RedPandaHome


@pytest.mark.parametrize("with_project", [False, True])
def test_fresh_entry_refuses_a_data_root_with_agents(tmp_path, monkeypatch, with_project):
    home = RedPandaHome(tmp_path / "home")
    directory = (
        home.agent_worktrees_root / "project" / "agent"
        if with_project
        else home.agent_directories_root / "agent"
    )
    directory.mkdir(parents=True)
    personal_file = directory / "note.txt"
    personal_file.write_bytes(b"keep bot files")
    home.runtime_sessions_root.mkdir(parents=True)
    journal = home.runtime_sessions_root / "agent.jsonl"
    journal.write_bytes(b"keep bot conversation")
    monkeypatch.setenv("REDPANDA_HOME", str(home.root))
    monkeypatch.setattr(bootstrap, "install_host_environment", lambda: None)
    monkeypatch.setattr(bootstrap, "install_timing_logging", lambda: None)

    async def start():
        async with bootstrap.bootstrap_assistant(lambda *_: None, fresh_sessions=True):
            pass

    with pytest.raises(bootstrap.DataRootHasAgents, match=re.escape(str(home.root))):
        asyncio.run(start())
    assert personal_file.read_bytes() == b"keep bot files"
    assert journal.read_bytes() == b"keep bot conversation"
