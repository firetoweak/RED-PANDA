from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


HOME_PATH_ENV = "HELPERME_HOME"


@dataclass(frozen=True, slots=True)
class HelperMeHome:
    """HelperMe 自身的持久数据目录，不是 Agent 任务 Workspace。"""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())

    @classmethod
    def default(cls) -> "HelperMeHome":
        if HOME_PATH_ENV in os.environ:
            return cls(Path(os.environ[HOME_PATH_ENV]))
        return cls(Path.home() / ".helperme")

    @property
    def sessions_root(self) -> Path:
        return self.root / "sessions"

    @property
    def config_path(self) -> Path:
        return self.root / "config.json"

    @property
    def workspaces_path(self) -> Path:
        return self.root / "workspaces.json"

    @property
    def mcp_root(self) -> Path:
        return self.root / "mcp"

    @property
    def skills_root(self) -> Path:
        return self.root / "skills"

    @property
    def clis_root(self) -> Path:
        return self.root / "clis"

    @property
    def state_root(self) -> Path:
        return self.root / "state"

    @property
    def runtime_sessions_root(self) -> Path:
        return self.root / "runtime_sessions"

    def initialize(self) -> None:
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        self.mcp_root.mkdir(parents=True, exist_ok=True)
        self.skills_root.mkdir(parents=True, exist_ok=True)
        self.clis_root.mkdir(parents=True, exist_ok=True)
        self.state_root.mkdir(parents=True, exist_ok=True)


def runtime_data_root() -> Path:
    root = HelperMeHome.default().runtime_sessions_root
    root.mkdir(parents=True, exist_ok=True)
    return root
