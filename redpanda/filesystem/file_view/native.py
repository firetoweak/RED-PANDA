"""Host file identity operations for the current platform."""
from __future__ import annotations

import os

if os.name == "nt":
    from .native_win import (
        directory_identity,
        identity,
        identity_handle,
        open_file,
        remove_open,
    )
else:
    from .native_posix import (
        directory_identity,
        identity,
        open_file,
        remove_open,
    )

__all__ = [
    "directory_identity",
    "identity",
    "open_file",
    "remove_open",
]
