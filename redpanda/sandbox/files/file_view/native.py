"""Host file identity and foreground command lifetime for the current platform."""
from __future__ import annotations

import os

if os.name == "nt":
    from .native_win import (
        Running,
        directory_identity,
        identity,
        identity_handle,
        open_file,
        remove_open,
    )
else:
    from .native_posix import (
        Running,
        directory_identity,
        identity,
        open_file,
        remove_open,
    )

__all__ = [
    "Running",
    "directory_identity",
    "identity",
    "open_file",
    "remove_open",
]
