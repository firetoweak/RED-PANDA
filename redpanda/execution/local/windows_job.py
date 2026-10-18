from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes


JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_BASIC_PROCESS_ID_LIST_CLASS = 3
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
ERROR_INVALID_PARAMETER = 87
ERROR_MORE_DATA = 234
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = (
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.AssignProcessToJobObject.argtypes = (
        wintypes.HANDLE,
        wintypes.HANDLE,
    )
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p)
    _kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    _kernel32.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
    _kernel32.IsProcessInJob.restype = wintypes.BOOL
    _kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
else:
    _kernel32 = None


class WindowsJob:
    def __init__(self, handle: int) -> None:
        self._handle = handle

    @classmethod
    def create(cls) -> "WindowsJob":
        if _kernel32 is None:
            raise OSError("Windows Job Object 只在 Windows 上可用")
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if not _kernel32.SetInformationJobObject(
            handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            _kernel32.CloseHandle(handle)
            raise error
        return cls(handle)

    def assign(self, pid: int) -> None:
        assert _kernel32 is not None
        process = _kernel32.OpenProcess(
            PROCESS_TERMINATE | PROCESS_SET_QUOTA,
            False,
            pid,
        )
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not _kernel32.AssignProcessToJobObject(self._handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            _kernel32.CloseHandle(process)

    def close(self) -> None:
        assert _kernel32 is not None
        # A terminated process leaves the Job before it has closed its handles,
        # so the Job's counters cannot tell when it is gone. Freeze membership,
        # hold every member, then wait for the members themselves.
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        )
        limits.BasicLimitInformation.ActiveProcessLimit = 1
        if not _kernel32.SetInformationJobObject(
            self._handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        members = []
        try:
            for pid in self._process_ids():
                process = _kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                if not process:
                    error = ctypes.get_last_error()
                    if error != ERROR_INVALID_PARAMETER:
                        raise ctypes.WinError(error)
                    continue  # Already fully gone.
                inside = wintypes.BOOL()
                if not _kernel32.IsProcessInJob(process, self._handle, ctypes.byref(inside)):
                    error = ctypes.get_last_error()
                    _kernel32.CloseHandle(process)
                    raise ctypes.WinError(error)
                if inside:
                    members.append(process)
                else:  # The id was reused after its member was fully gone.
                    _kernel32.CloseHandle(process)
            if not _kernel32.TerminateJobObject(self._handle, 130):
                raise ctypes.WinError(ctypes.get_last_error())
            deadline = time.monotonic() + 10
            for process in members:
                remaining = max(0, round((deadline - time.monotonic()) * 1000))
                result = _kernel32.WaitForSingleObject(process, remaining)
                if result == WAIT_TIMEOUT:
                    raise TimeoutError("Windows task Job did not drain")
                if result != WAIT_OBJECT_0:
                    raise ctypes.WinError(ctypes.get_last_error())
        finally:
            for process in members:
                _kernel32.CloseHandle(process)
        if not _kernel32.CloseHandle(self._handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def _process_ids(self) -> list[int]:
        assert _kernel32 is not None
        count = 1
        while True:
            class ProcessIdList(ctypes.Structure):
                _fields_ = [("assigned", wintypes.DWORD), ("listed", wintypes.DWORD),
                            ("ids", ctypes.c_size_t * count)]
            listing = ProcessIdList()
            if _kernel32.QueryInformationJobObject(
                self._handle, JOB_OBJECT_BASIC_PROCESS_ID_LIST_CLASS,
                ctypes.byref(listing), ctypes.sizeof(listing), None,
            ):
                return list(listing.ids[:listing.listed])
            error = ctypes.get_last_error()
            if error != ERROR_MORE_DATA:
                raise ctypes.WinError(error)
            count = listing.assigned
