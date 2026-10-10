"""Start a Session Worker whose stdio is not the Channel pipe."""

from __future__ import annotations

import os
from contextlib import contextmanager

from redpanda.assistant.host.process_env import install_host_environment


def start_worker(process, extra_handles=()) -> None:
    """Start ``process`` without inheriting Channel stdin/stdout/stderr.

    A Worker must be able to boot when the Host has no console, as with ACP
    stdio. Its own stdin/stdout/stderr are attached to the platform null
    device so Channel framing stays on the Host. The child inherits the product
    environment; its workspace command environment is passed separately.
    """
    install_host_environment()
    _disinherit_handles((process, *extra_handles))
    if os.name == "nt":
        with _windows_nul_stdio():
            process.start()
        return
    process.start()


def _disinherit_handles(objects) -> None:
    seen: set[int] = set()
    # Windows Connection.fileno() yields an OS handle; POSIX yields a CRT fd.
    set_not_inheritable = (
        os.set_handle_inheritable if os.name == "nt" else os.set_inheritable
    )
    for obj in objects:
        for candidate in _handle_owners(obj):
            fileno = getattr(candidate, "fileno", None)
            if not callable(fileno):
                continue
            handle = fileno()
            if type(handle) is not int or handle in seen:
                continue
            seen.add(handle)
            try:
                set_not_inheritable(handle, False)
            except OSError:
                continue


def _handle_owners(obj):
    yield obj
    args = getattr(obj, "_args", None)
    if args:
        yield from args
    kwargs = getattr(obj, "_kwargs", None)
    if kwargs:
        yield from kwargs.values()


@contextmanager
def _windows_nul_stdio():
    import msvcrt
    import subprocess
    import threading
    import _winapi

    original = _winapi.CreateProcess
    spawning_thread = threading.get_ident()
    nul_in = os.open("NUL", os.O_RDONLY)
    nul_out = os.open("NUL", os.O_WRONLY)
    saved: list[tuple[int, bool]] = []
    try:
        os.set_inheritable(nul_in, True)
        os.set_inheritable(nul_out, True)
        for fd in (0, 1, 2):
            try:
                saved.append((fd, os.get_inheritable(fd)))
                os.set_inheritable(fd, False)
            except OSError:
                continue

        def create_process(
            application_name,
            command_line,
            proc_attrs,
            thread_attrs,
            inherit_handles,
            creation_flags,
            env,
            cwd,
            startupinfo,
        ):
            # Git 快照等后台线程仍须保留自己的 PIPE，不能被 Worker 的 NUL 吞掉。
            if threading.get_ident() != spawning_thread:
                return original(
                    application_name, command_line, proc_attrs, thread_attrs,
                    inherit_handles, creation_flags, env, cwd, startupinfo,
                )
            info = subprocess.STARTUPINFO()
            info.dwFlags = (
                subprocess.STARTF_USESTDHANDLES
                | subprocess.STARTF_FORCEOFFFEEDBACK
            )
            info.hStdInput = msvcrt.get_osfhandle(nul_in)
            info.hStdOutput = msvcrt.get_osfhandle(nul_out)
            info.hStdError = msvcrt.get_osfhandle(nul_out)
            # 只继承这两个 NUL 句柄，避免持有并发子进程的管道而阻止 EOF。
            info.lpAttributeList = {"handle_list": [info.hStdInput, info.hStdOutput]}
            return original(
                application_name,
                command_line,
                proc_attrs,
                thread_attrs,
                True,
                creation_flags,
                env,
                cwd,
                info,
            )

        _winapi.CreateProcess = create_process
        try:
            yield
        finally:
            _winapi.CreateProcess = original
    finally:
        for fd, inheritable in saved:
            try:
                os.set_inheritable(fd, inheritable)
            except OSError:
                continue
        os.close(nul_in)
        os.close(nul_out)
