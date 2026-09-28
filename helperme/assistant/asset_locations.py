from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from helperme.runtime import SqliteJournal


def inherited_drawers(root: Path, session_id: str, drawer: str) -> tuple[Path, ...]:
    key = sha256(session_id.encode("utf-8")).hexdigest()
    journal = root / key / "journal.sqlite"
    if not journal.is_file():
        return ()
    return tuple(
        source.parent / drawer
        for source in SqliteJournal.prefix_paths_at(journal)
    )
