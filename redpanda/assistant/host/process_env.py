"""Give Host and Worker the current user's session environment.

Channel is transport. A GUI client may start RED PANDA with a leftover PATH.
The process that runs Sessions must see the user session environment, with
Channel-declared variables kept as overlays.
"""

from __future__ import annotations

import os
from collections.abc import Mapping


def host_process_environment(
    current: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if current is None else current
    result = {name: value for name, value in source.items()}
    session = user_session_environment()
    if session is None:
        return result
    # Windows environment names are case-insensitive, unlike ordinary dict keys.
    result = {name.upper(): value for name, value in result.items()}
    session = {name.upper(): value for name, value in session.items()}
    for name, value in session.items():
        if not result.get(name):
            result[name] = value
    result["PATH"] = _merge_path(result.get("PATH", ""), session.get("PATH", ""))
    return result


def install_host_environment() -> None:
    os.environ.update(host_process_environment())


def user_session_environment() -> dict[str, str] | None:
    if os.name != "nt":
        return None
    return _windows_user_environment()


def _merge_path(current: str, session: str) -> str:
    seen: set[str] = set()
    parts: list[str] = []
    for raw in (*current.split(os.pathsep), *session.split(os.pathsep)):
        item = raw.strip()
        if not item:
            continue
        key = os.path.normcase(os.path.normpath(item))
        if key in seen:
            continue
        seen.add(key)
        parts.append(item)
    return os.pathsep.join(parts)


def _windows_user_environment() -> dict[str, str]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    get_process = kernel32.GetCurrentProcess
    get_process.argtypes = []
    get_process.restype = wintypes.HANDLE
    open_token = advapi32.OpenProcessToken
    open_token.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    open_token.restype = wintypes.BOOL
    close = kernel32.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    create = userenv.CreateEnvironmentBlock
    create.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.HANDLE,
        wintypes.BOOL,
    ]
    create.restype = wintypes.BOOL
    destroy = userenv.DestroyEnvironmentBlock
    destroy.argtypes = [ctypes.c_void_p]
    destroy.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    # CreateEnvironmentBlock requires QUERY | DUPLICATE for a primary token.
    if not open_token(get_process(), 0x0008 | 0x0002, ctypes.byref(token)):
        raise OSError(ctypes.get_last_error(), "OpenProcessToken failed")
    try:
        block = ctypes.c_void_p()
        if not create(ctypes.byref(block), token, False):
            raise OSError(ctypes.get_last_error(), "CreateEnvironmentBlock failed")
        try:
            return _parse_environment_block(block.value)
        finally:
            destroy(block)
    finally:
        close(token)


def _parse_environment_block(address: int | None) -> dict[str, str]:
    if not address:
        raise OSError("CreateEnvironmentBlock returned an empty block")
    import ctypes

    result: dict[str, str] = {}
    offset = 0
    wchar = ctypes.sizeof(ctypes.c_wchar)
    while True:
        entry = ctypes.wstring_at(address + offset)
        if not entry:
            break
        name, separator, value = entry.partition("=")
        if separator and name:
            result[name] = value
        offset += (len(entry) + 1) * wchar
    if not result:
        raise OSError("CreateEnvironmentBlock returned an empty block")
    return result
