"""Native attribute requests, independent of the filesystem implementation."""
import ctypes
from ctypes import wintypes


def kernel():
    value = ctypes.WinDLL("kernel32", use_last_error=True)
    value.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    value.GetFileAttributesW.restype = wintypes.DWORD
    value.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    value.SetFileAttributesW.restype = wintypes.BOOL
    return value


def flags(path):
    value = kernel().GetFileAttributesW(str(path))
    if value == 0xFFFFFFFF:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


def set_flag(path, flag, enabled):
    value = flags(path) & ~0x80
    value = value | flag if enabled else value & ~flag
    if not kernel().SetFileAttributesW(str(path), value or 0x80):
        raise ctypes.WinError(ctypes.get_last_error())
