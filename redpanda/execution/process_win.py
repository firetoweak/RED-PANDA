"""Managed raw Windows task process lifetime."""
from __future__ import annotations
import ctypes as c
from ctypes import wintypes as w
import msvcrt
import os
import subprocess
import time
from pathlib import Path

from redpanda.execution.local.windows_job import WindowsJob

k = c.WinDLL('kernel32', use_last_error=True)

def api(name, args, result):
    fn = getattr(k, name)
    fn.argtypes, fn.restype = args, result
    return fn

def checked(ok):
    if not ok:
        raise c.WinError(c.get_last_error())
    return ok

close_handle = api('CloseHandle', [w.HANDLE], w.BOOL)

class Startup(c.Structure):
    _fields_ = [('cb',w.DWORD),('reserved',w.LPWSTR),('desktop',w.LPWSTR),('title',w.LPWSTR),
        ('x',w.DWORD),('y',w.DWORD),('xsize',w.DWORD),('ysize',w.DWORD),('xchars',w.DWORD),('ychars',w.DWORD),
        ('fill',w.DWORD),('flags',w.DWORD),('show',w.WORD),('reserved2len',w.WORD),('reserved2',c.c_void_p),
        ('stdin',w.HANDLE),('stdout',w.HANDLE),('stderr',w.HANDLE)]
class ProcessInfo(c.Structure):
    _fields_ = [('process',w.HANDLE),('thread',w.HANDLE),('pid',w.DWORD),('tid',w.DWORD)]
create_process = api('CreateProcessW',[w.LPCWSTR,w.LPWSTR,c.c_void_p,c.c_void_p,w.BOOL,w.DWORD,c.c_void_p,w.LPCWSTR,c.POINTER(Startup),c.POINTER(ProcessInfo)],w.BOOL)
resume_thread = api('ResumeThread',[w.HANDLE],w.DWORD)
wait_object = api('WaitForSingleObject',[w.HANDLE,w.DWORD],w.DWORD)
exit_code = api('GetExitCodeProcess',[w.HANDLE,c.POINTER(w.DWORD)],w.BOOL)
terminate_process = api('TerminateProcess',[w.HANDLE,w.UINT],w.BOOL)

class Running:
    def __init__(self,argv, cwd, environment, log_dir):
        self.job = WindowsJob.create()
        self.info = ProcessInfo()
        self.logs = []
        try:
            for name, mode in [('stdin.bin','rb'),('stdout.bin','wb'),('stderr.bin','wb')]:
                path = Path(log_dir)/name
                if mode == 'rb': path.write_bytes(b'')
                stream = path.open(mode)
                os.set_inheritable(stream.fileno(),True)
                self.logs.append(stream)
            startup = Startup()
            startup.cb, startup.flags = c.sizeof(startup), 0x100
            startup.stdin,startup.stdout,startup.stderr = [msvcrt.get_osfhandle(s.fileno()) for s in self.logs]
            env = c.create_unicode_buffer('\0'.join(f'{key}={value}' for key,value in sorted(environment.items(),key=lambda p:p[0].upper()))+'\0\0')
            command = c.create_unicode_buffer(subprocess.list2cmdline([str(a) for a in argv]))
            # No user instruction runs before the process belongs to the Job.
            checked(create_process(str(argv[0]),command,None,None,True,4|0x400|0x08000000,env,str(cwd),c.byref(startup),c.byref(self.info)))
            self.job.assign(self.info.pid)
            if resume_thread(self.info.thread) == 0xffffffff:
                raise c.WinError(c.get_last_error())
        except BaseException:
            self.close()
            raise
        finally:
            for stream in self.logs:
                if not stream.closed: os.set_inheritable(stream.fileno(),False)

    def wait(self, timeout=60, cancel_event=None):
        start = time.monotonic()
        timed_out = False
        interrupted = False
        while wait_object(self.info.process,20) == 0x102:
            if cancel_event is not None and cancel_event.is_set():
                interrupted=True
                self.job.terminate(130)
                break
            if time.monotonic()-start >= timeout:
                timed_out = True
                self.job.terminate(124)
                break
        checked(wait_object(self.info.process,10000) == 0)
        code = w.DWORD()
        checked(exit_code(self.info.process,c.byref(code)))
        # Shell exit does not make surviving descendants a completed foreground command.
        self.job.terminate(125)
        self.job.drain()
        self.close()
        return {'exit_code':code.value,'timed_out':timed_out,'interrupted':interrupted,'duration_ms':(time.monotonic()-start)*1000}

    def close(self):
        if self.info.process and wait_object(self.info.process,0)==0x102:
            checked(terminate_process(self.info.process,130))
            checked(wait_object(self.info.process,10000)==0)
        if self.job is not None:
            self.job.release()
            self.job = None
        for handle in [self.info.thread,self.info.process]:
            if handle: checked(close_handle(handle))
        self.info.thread,self.info.process = None,None
        for stream in self.logs:
            if not stream.closed:
                stream.flush()
                if stream.writable(): os.fsync(stream.fileno())
                stream.close()
