from __future__ import annotations

import json
from pathlib import Path


class SessionFlagStore:
    """Host 持有的 Session 级开关。缺省 false，只在拨过时落盘，不进 Journal。"""

    def __init__(self, root: Path | None, filename: str) -> None:
        self._root = root
        self._filename = filename
        self._values: dict[str, bool] = {}
        self._load()

    def _load(self) -> None:
        if self._root is None:
            return
        path = self._root / self._filename
        if not path.is_file():
            return
        raw = json.loads(path.read_text(encoding="utf-8"))
        if type(raw) is not dict:
            raise ValueError(f"{self._filename} must be an object")
        self._values = {str(key): bool(value) for key, value in raw.items()}

    def _save(self) -> None:
        if self._root is None:
            return
        (self._root / self._filename).write_text(
            json.dumps(self._values, ensure_ascii=False),
            encoding="utf-8",
        )

    def get(self, session_id: str, default: bool = False) -> bool:
        return self._values.get(session_id, default)

    def remember(self, session_id: str, value: bool) -> None:
        self._values[session_id] = bool(value)

    def set(self, session_id: str, value: bool) -> None:
        self.remember(session_id, value)
        self._save()
