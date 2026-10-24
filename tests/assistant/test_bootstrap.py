"""Overdue clocks cannot deliver before startup recovery takes over old attempts."""
import asyncio
from datetime import datetime, timedelta, timezone

from konden import bootstrap
from konden.automation.once import OneShotSchedules, TIME_REACHED
from konden.config import AppConfig
from konden.paths import KondenHome


def test_overdue_clock_waits_for_startup_recovery(tmp_path, monkeypatch):
    home = KondenHome(tmp_path / "home")
    home.initialize()
    OneShotSchedules(home.state_root / "automation.sqlite").register(
        "due", "bot", 1, "observe", started_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    monkeypatch.setenv("KONDEN_HOME", str(home.root))
    monkeypatch.setenv("KONDEN_CONFIG", str(home.config_path))
    monkeypatch.setattr(bootstrap, "install_host_environment", lambda: None)
    monkeypatch.setattr(bootstrap, "install_timing_logging", lambda: None)

    async def check():
        delivered = asyncio.Event()
        recovered = asyncio.Event()

        async def recover(host):
            # Let an already-started overdue clock run, if there is one.
            await asyncio.sleep(0)
            assert not delivered.is_set()
            recovered.set()

        async def receive(host, operation, session_id, arguments):
            assert operation == "fact" and arguments["fact_type"] == TIME_REACHED
            assert recovered.is_set()
            delivered.set()

        monkeypatch.setattr(bootstrap.HostSupervisor, "resume_unfinished_sessions", recover)
        monkeypatch.setattr(bootstrap.HostSupervisor, "request", receive)
        async with bootstrap.bootstrap_assistant(lambda *_: None, app_config=AppConfig(None, ())):
            await asyncio.wait_for(delivered.wait(), 5)

    asyncio.run(check())
