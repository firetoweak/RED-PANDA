"""Host file identity operations for the current platform."""
from __future__ import annotations

import os

if os.name == "nt":
    from .native_win import (
        directory_metadata,
        directory_identity,
        hidden,
        identity,
        identity_handle,
        open_file,
        remove_open,
        set_hidden,
        set_path_hidden,
    )
else:
    from .native_posix import (
        directory_identity,
        identity,
        open_file,
        remove_open,
    )

    def hidden(file):
        return None

    def directory_metadata(path):
        return directory_identity(path), None

__all__ = [
    "directory_identity",
    "identity",
    "open_file",
    "remove_open",
]
