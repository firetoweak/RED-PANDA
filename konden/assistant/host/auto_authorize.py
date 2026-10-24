from __future__ import annotations

import json
from pathlib import Path

from konden.config import write_json


class AutoAuthorizeStore:
    """所有会话共用的工具授权偏好，不进入会话日志。"""

    def __init__(self, root: Path | None) -> None:
        self.path = None if root is None else root / "authorization.json"
        self.enabled = True
        if self.path is not None and self.path.is_file():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if type(data) is not dict or set(data) != {"enabled"} or type(data["enabled"]) is not bool:
                raise ValueError("authorization.json must contain only enabled (bool)")
            self.enabled = data["enabled"]

    def set(self, enabled: bool) -> None:
        if enabled == self.enabled:
            return
        if self.path is not None:
            write_json(self.path, {"enabled": enabled})
        self.enabled = enabled
