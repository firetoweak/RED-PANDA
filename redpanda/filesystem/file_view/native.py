"""Host file identity operations for the current platform."""
from __future__ import annotations

import os

if os.name == "nt":
    from .native_win import (
        directory_metadata,
        directory_identity,
        attributes,
        identity,
        identity_handle,
        open_file,
        remove_open,
        set_attributes,
        set_path_attributes,
    )
else:
    from .native_posix import (
        directory_identity,
        identity,
        open_file,
        remove_open,
        set_path_mode,
    )

    def attributes(file):
        return {"hidden": None, "readonly": None}

    def directory_metadata(path):
        return directory_identity(path), {"hidden": None, "readonly": None}

__all__ = [
    "directory_identity",
    "identity",
    "open_file",
    "remove_open",
]
