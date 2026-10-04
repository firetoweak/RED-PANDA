from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class _SessionMap:
    """Host 持有的 Session 级元数据。只在人做过选择时写入，不进 Journal。"""

    def __init__(self, root: Path | None, filename: str) -> None:
        self._root = root
        self._filename = filename
        self._values: dict[str, Any] = {}
        self._load()

    def _coerce(self, value: Any) -> Any:
        raise NotImplementedError

    def _load(self) -> None:
        if self._root is None:
            return
        path = self._root / self._filename
        if not path.is_file():
            return
        raw = json.loads(path.read_text(encoding="utf-8"))
        if type(raw) is not dict:
            raise ValueError(f"{self._filename} must be an object")
        self._values = {str(key): self._coerce(value) for key, value in raw.items()}

    def _save(self) -> None:
        if self._root is None:
            return
        (self._root / self._filename).write_text(
            json.dumps(self._values, ensure_ascii=False),
            encoding="utf-8",
        )


class SessionFlagStore(_SessionMap):
    """Session 级开关。缺省 false，只在人拨过时落盘。"""

    def _coerce(self, value: Any) -> bool:
        return bool(value)

    def get(self, session_id: str) -> bool:
        return self._values.get(session_id, False)

    def remember(self, session_id: str, value: bool) -> None:
        self._values[session_id] = bool(value)

    def set(self, session_id: str, value: bool) -> None:
        self.remember(session_id, value)
        self._save()


class SessionLineageStore(_SessionMap):
    """会话线：原地改写产生的新身份，以及它顶掉的那个身份。

    侧栏列的是线，不是 journal。被顶掉的身份照常可读，只是不再代表这条线。
    """

    def _coerce(self, value: Any) -> str:
        if type(value) is not str or not value:
            raise ValueError(f"{self._filename} values must be session ids")
        return value

    def supersede(self, session_id: str, replaced: str) -> None:
        self._values[session_id] = self._coerce(replaced)
        self._save()

    def is_superseded(self, session_id: str) -> bool:
        return session_id in self._values.values()


class SessionTextStore(_SessionMap):
    """Session 级文案。缺省没有，只在人写过时落盘。"""

    def _coerce(self, value: Any) -> str:
        if type(value) is not str or not value.strip():
            raise ValueError(f"{self._filename} values must be non-empty strings")
        return value.strip()

    def get(self, session_id: str) -> str | None:
        value = self._values.get(session_id)
        return value if type(value) is str else None

    def items(self) -> dict[str, str]:
        return dict(self._values)

    def set(self, session_id: str, title: str) -> None:
        self._values[session_id] = self._coerce(title)
        self._save()
