"""Windows handles: exclusive host file changes."""
from __future__ import annotations
import ctypes as c
from ctypes import wintypes as w
import msvcrt
import os
from pathlib import Path

k = c.WinDLL('kernel32', use_last_error=True)
INVALID = c.c_void_p(-1).value

def api(name, args, result):
    fn = getattr(k, name)
    fn.argtypes, fn.restype = args, result
    return fn

create_file = api('CreateFileW', [w.LPCWSTR,w.DWORD,w.DWORD,c.c_void_p,w.DWORD,w.DWORD,w.HANDLE], w.HANDLE)
close_handle = api('CloseHandle', [w.HANDLE], w.BOOL)
get_info = api('GetFileInformationByHandleEx', [w.HANDLE,c.c_int,c.c_void_p,w.DWORD], w.BOOL)
set_info = api('SetFileInformationByHandle', [w.HANDLE,c.c_int,c.c_void_p,w.DWORD], w.BOOL)

def checked(ok):
    if not ok:
        raise c.WinError(c.get_last_error())
    return ok

class FileId(c.Structure):
    _fields_ = [('volume',c.c_ulonglong),('file_id',c.c_ubyte*16)]

def identity(file):
    return identity_handle(msvcrt.get_osfhandle(file.fileno()))

def identity_handle(handle):
    info = FileId()
    checked(get_info(handle,18,c.byref(info),c.sizeof(info)))
    return (info.volume.to_bytes(8,'little') + bytes(info.file_id)).hex()

def directory_identity(path):
    handle = create_file(str(path),0x80,0,None,3,0x2200000,None)
    if handle == INVALID: raise c.WinError(c.get_last_error())
    try: return identity_handle(handle)
    finally: checked(close_handle(handle))

def open_file(path: Path, *, create=False, write=False, shared_read=False):
    # OPEN_REPARSE_POINT prevents treating a link as an ordinary file.
    handle = create_file(str(path),0x80000000 | (0x40000000 | 0x10000 if write else 0),1 if shared_read else 0,None,1 if create else 3,0x200000,None)
    if handle == INVALID:
        raise c.WinError(c.get_last_error())
    fd = msvcrt.open_osfhandle(handle,os.O_BINARY | (os.O_RDWR if write else os.O_RDONLY))
    return os.fdopen(fd,'r+b' if write else 'rb')

def remove_open(file):
    flag = w.BOOL(True)
    checked(set_info(msvcrt.get_osfhandle(file.fileno()),4,c.byref(flag),c.sizeof(flag)))
