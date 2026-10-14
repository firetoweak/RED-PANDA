"""Runs inside an injected process; only scratch fixtures are touched."""
import ctypes as c
from ctypes import wintypes as w
import mmap
import os
from pathlib import Path
import subprocess
import sys

case, host_text, outside_text, *extra = sys.argv[1:]
host, outside = Path(host_text), Path(outside_text)

def blocked(action):
    try:
        action()
    except PermissionError:
        return
    except OSError as error:
        if error.winerror == 5:
            return
        raise
    raise AssertionError("host write was permitted")

if case == "host-read":
    assert (host / "base.txt").read_text() == "BASE"
    assert Path.cwd() == host
elif case == "host-mutations":
    blocked(lambda: (host / "new.txt").write_text("BAD"))
    blocked(lambda: (host / "base.txt").write_text("BAD"))
    blocked(lambda: (host / "base.txt").open("ab"))
    blocked(lambda: (host / "base.txt").unlink())
    blocked(lambda: (host / "base.txt").rename(outside / "stolen.txt"))
    blocked(lambda: (host / "subdir").mkdir())
    blocked(lambda: (host / "empty").rmdir())
    blocked(lambda: os.chmod(host / "base.txt", 0o444))
    blocked(lambda: os.utime(host / "base.txt", (1, 1)))
    source = outside / "move.txt"
    source.write_text("OTHER")
    blocked(lambda: source.replace(host / "base.txt"))
    blocked(lambda: os.link(source, host / "new-link.txt"))
elif case == "path-spellings":
    blocked(lambda: Path("\\\\?\\" + str(host / "base.txt")).write_text("BAD"))
    blocked(lambda: Path(str(host).upper(), "BASE.TXT").write_text("BAD"))
    blocked(lambda: Path("..", host.name, "base.txt").write_text("BAD"))
    blocked(lambda: Path(str(host / "base.txt") + ":stream").write_text("BAD"))
    sibling = host.with_name(host.name + "-other")
    (sibling / "ok.txt").write_text("GOOD")
    assert (sibling / "ok.txt").read_text() == "GOOD"
elif case == "mount":
    assert Path("base.txt").read_text() == "BASE"
    Path("base.txt").write_text("VIEW")
    assert Path("base.txt").read_text() == "VIEW"
    assert (host / "base.txt").read_text() == "BASE"
    Path("新文件.txt").write_text("NEW", encoding="utf-8")
    Path("directory").mkdir()
    Path("directory/entry").write_text("ENTRY")
    Path("directory/entry").rename("directory/renamed")
    Path("directory/renamed").unlink()
    blocked(lambda: (host / "base.txt").write_text("BAD"))
elif case == "child":
    env = {k:v for k,v in os.environ.items() if k in ("SystemRoot", "PATH", "TEMP", "TMP")}
    subprocess.run([sys.executable, __file__, "host-mutations", host_text, outside_text], env=env, check=True)
    subprocess.run([sys.executable, "-c", "from pathlib import Path;Path('child.txt').write_text('CHILD')"], env=env, check=True)
    assert Path("child.txt").read_text() == "CHILD"
elif case == "grandchild":
    subprocess.run([sys.executable,__file__,"child",host_text,outside_text],check=True)
elif case == "junction":
    blocked(lambda: Path(extra[0],"base.txt").write_text("BAD"))
    blocked(lambda: Path(extra[0],"new-via-junction").write_text("BAD"))
elif case == "orphan-child":
    child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(20)"])
    (outside/"orphan-pid.txt").write_text(str(child.pid))
elif case == "process-a":
    class SI(c.Structure):
        _fields_=[("cb",w.DWORD),("reserved",w.LPSTR),("desktop",w.LPSTR),("title",w.LPSTR),("x",w.DWORD),("y",w.DWORD),("xs",w.DWORD),("ys",w.DWORD),("xc",w.DWORD),("yc",w.DWORD),("fill",w.DWORD),("flags",w.DWORD),("show",w.WORD),("reserved2size",w.WORD),("reserved2",c.c_void_p),("input",w.HANDLE),("output",w.HANDLE),("error",w.HANDLE)]
    class PI(c.Structure):
        _fields_=[("process",w.HANDLE),("thread",w.HANDLE),("pid",w.DWORD),("tid",w.DWORD)]
    kernel=c.WinDLL("kernel32",use_last_error=True)
    fn=kernel.CreateProcessA
    fn.argtypes=[w.LPCSTR,w.LPSTR,c.c_void_p,c.c_void_p,w.BOOL,w.DWORD,c.c_void_p,w.LPCSTR,c.POINTER(SI),c.POINTER(PI)]
    # ASCII command text; target host path comes from the existing UTF-8 script's
    # arguments via Unicode escape literals rather than an ANSI path conversion.
    code=f"from pathlib import Path;\ntry: Path({str(host / 'base.txt')!a}).write_text('BAD')\nexcept PermissionError: pass\nelse: raise AssertionError('unhooked child')"
    cmd=subprocess.list2cmdline([sys.executable,"-c",code]).encode("ascii")
    si=SI();si.cb=c.sizeof(si);pi=PI()
    assert fn(str(sys.executable).encode("ascii"),c.create_string_buffer(cmd),None,None,False,0,None,None,c.byref(si),c.byref(pi)),c.get_last_error()
    kernel.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD]
    kernel.GetExitCodeProcess.argtypes=[w.HANDLE,c.POINTER(w.DWORD)]
    kernel.CloseHandle.argtypes=[w.HANDLE]
    kernel.WaitForSingleObject(pi.process,10000);status=w.DWORD()
    assert kernel.GetExitCodeProcess(pi.process,c.byref(status));assert status.value==0,status.value
    kernel.CloseHandle(pi.thread);kernel.CloseHandle(pi.process)
elif case == "inherited-write":
    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.WriteFile.argtypes = [w.HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.c_void_p]
    written = w.DWORD()
    assert not kernel.WriteFile(int(extra[0]), c.create_string_buffer(b"BAD"), 3, c.byref(written), None)
    assert c.get_last_error() == 5
    kernel.CreateFileMappingW.restype = w.HANDLE
    kernel.CreateFileMappingW.argtypes = [w.HANDLE,c.c_void_p,w.DWORD,w.DWORD,w.DWORD,w.LPCWSTR]
    assert not kernel.CreateFileMappingW(int(extra[0]),None,4,0,0,None)
    assert c.get_last_error() == 5
elif case == "suspended-child":
    marker = outside / "suspended-ran.txt"
    try:
        subprocess.Popen([sys.executable,"-c",f"open({str(marker)!r},'w').write('BAD')"], creationflags=4)
    except OSError as error:
        assert error.winerror == 50
    else:
        raise AssertionError("unsupported suspended child was accepted")
    assert not marker.exists()
elif case == "x86-child":
    marker = outside / "x86-ran.txt"
    try:
        subprocess.run([str(Path(os.environ["SystemRoot"]) / "SysWOW64/cmd.exe"),"/d","/c",f'echo BAD > "{marker}"'],check=True)
    except OSError as error:
        assert error.winerror == 50
    else:
        raise AssertionError("unsupported x86 child was accepted")
    assert not marker.exists()
elif case == "native-relative":
    class U(c.Structure):
        _fields_ = [("Length",w.USHORT),("MaximumLength",w.USHORT),("Buffer",w.LPWSTR)]
    class A(c.Structure):
        _fields_ = [("Length",w.ULONG),("RootDirectory",w.HANDLE),("ObjectName",c.POINTER(U)),("Attributes",w.ULONG),("SecurityDescriptor",c.c_void_p),("SecurityQualityOfService",c.c_void_p)]
    class IO(c.Structure):
        _fields_ = [("Status",c.c_void_p),("Information",c.c_size_t)]
    kernel = c.WinDLL("kernel32",use_last_error=True)
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CreateFileW.argtypes = [w.LPCWSTR,w.DWORD,w.DWORD,c.c_void_p,w.DWORD,w.DWORD,w.HANDLE]
    handle = kernel.CreateFileW(str(host),0x80,7,None,3,0x02000000,None)
    assert handle != c.c_void_p(-1).value
    kernel.CloseHandle.argtypes = [w.HANDLE]
    name = c.create_unicode_buffer("base.txt")
    u = U(16,18,c.cast(name,w.LPWSTR)); a = A(c.sizeof(A),handle,c.pointer(u),64,None,None)
    io=IO(); target=w.HANDLE()
    nt = c.WinDLL("ntdll").NtCreateFile
    nt.restype = w.LONG
    nt.argtypes = [c.POINTER(w.HANDLE),w.ULONG,c.POINTER(A),c.POINTER(IO),c.c_void_p,w.ULONG,w.ULONG,w.ULONG,w.ULONG,c.c_void_p,w.ULONG]
    result = nt(c.byref(target),0x40000000,c.byref(a),c.byref(io),None,0,7,1,0,None,0)
    kernel.CloseHandle(handle)
    assert result & 0xffffffff == 0xc0000022, hex(result & 0xffffffff)
else:
    raise AssertionError(case)
print("OK", case, flush=True)
