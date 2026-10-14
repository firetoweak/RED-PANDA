"""One-off Silo/Bind Filter experiment; no production integration or global binds."""
from __future__ import annotations

import argparse
import ctypes as c
from ctypes import wintypes as w
import hashlib
import json
import msvcrt
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback

WORKSPACE = Path("C:/LocalData/work/helpMe/RED-PANDA")
sys.path.insert(0, str(WORKSPACE))
sys.stdout.reconfigure(encoding="utf-8")
from redpanda.sandbox.files.file_view import Client
from redpanda.sandbox.files.file_view.client import native_executable
from redpanda.sandbox.files.file_view import native_win as n
from redpanda.sandbox.local.windows_job import WindowsJob, _kernel32

JOB_CREATE_SILO = 35
ATTR_HANDLE_LIST = 0x00020002
ATTR_JOB_LIST = 0x0002000D
SILO_BIND = 4

initialize_attrs = n.api("InitializeProcThreadAttributeList", [c.c_void_p,w.DWORD,w.DWORD,c.POINTER(c.c_size_t)],w.BOOL)
update_attrs = n.api("UpdateProcThreadAttribute", [c.c_void_p,w.DWORD,c.c_size_t,c.c_void_p,c.c_size_t,c.c_void_p,c.c_void_p],w.BOOL)
delete_attrs = n.api("DeleteProcThreadAttributeList", [c.c_void_p],None)
bind = c.WinDLL("bindfltapi.dll", use_last_error=True)
setup = bind.BfSetupFilter
setup.argtypes = [w.HANDLE,w.ULONG,w.LPCWSTR,w.LPCWSTR,c.POINTER(w.LPCWSTR),w.ULONG]
setup.restype = c.c_long

class StartupEx(c.Structure):
    _fields_ = [("startup",n.Startup),("attributes",c.c_void_p)]

class BindingDenied(OSError):
    def __init__(self, result):
        self.hresult = result & 0xffffffff
        super().__init__(self.hresult & 0xffff, c.FormatError(c.c_long(self.hresult).value).strip())

def make_silo():
    job = WindowsJob.create()
    try:
        n.checked(_kernel32.SetInformationJobObject(job._handle, JOB_CREATE_SILO, None, 0))
        return job
    except BaseException:
        job.close()
        raise

def apply_binding(job, logical, backing):
    # Non-null Silo handle + this flag: never create a global path binding.
    assert job._handle
    result = setup(job._handle, SILO_BIND, str(logical), str(backing), None, 0)
    if result < 0:
        raise BindingDenied(result)

def drain(job):
    n.checked(n.terminate_job(job._handle, 125))
    deadline = time.monotonic() + 10
    while True:
        info = n.Accounting()
        n.checked(n.query_job(job._handle, 1, c.byref(info), c.sizeof(info), None))
        if info.active == 0:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("Silo processes did not drain")
        time.sleep(.02)

def launch(job, folder, code, *args):
    info = n.ProcessInfo()
    size = c.c_size_t()
    initialize_attrs(None, 2, 0, c.byref(size))
    if c.get_last_error() != 122:
        raise c.WinError(c.get_last_error())
    buffer = c.create_string_buffer(size.value)
    n.checked(initialize_attrs(buffer, 2, 0, c.byref(size)))
    logs = []
    try:
        for name, mode in [("stdin.bin","wb+"),("stdout.bin","wb"),("stderr.bin","wb")]:
            stream = (folder / name).open(mode)
            os.set_inheritable(stream.fileno(), True)
            logs.append(stream)
        handles = (w.HANDLE * 3)(*[msvcrt.get_osfhandle(stream.fileno()) for stream in logs])
        jobs = (w.HANDLE * 1)(job._handle)
        n.checked(update_attrs(buffer,0,ATTR_HANDLE_LIST,handles,c.sizeof(handles),None,None))
        n.checked(update_attrs(buffer,0,ATTR_JOB_LIST,jobs,c.sizeof(jobs),None,None))
        startup = StartupEx()
        startup.startup.cb = c.sizeof(startup)
        startup.startup.flags = 0x100
        startup.startup.stdin,startup.startup.stdout,startup.startup.stderr = list(handles)
        startup.attributes = c.cast(buffer, c.c_void_p)
        environment = c.create_unicode_buffer("\0".join(
            f"{key}={value}" for key,value in sorted(os.environ.items(),key=lambda pair:pair[0].upper())
        ) + "\0\0")
        argv = [sys.executable, "-c", code, *map(str,args)]
        command = c.create_unicode_buffer(subprocess.list2cmdline(argv))
        n.checked(n.create_process(
            sys.executable,command,None,None,True,
            0x4 | 0x400 | 0x80000 | 0x08000000,
            environment,str(folder),c.cast(c.byref(startup),c.POINTER(n.Startup)),c.byref(info)))
        for stream in logs:
            os.set_inheritable(stream.fileno(), False)
        if n.resume_thread(info.thread) == 0xffffffff:
            raise c.WinError(c.get_last_error())
        wait = n.wait_object(info.process, 30000)
        if wait != 0:
            raise TimeoutError(f"Probe wait failed: {wait:#x}")
        code_out = w.DWORD()
        n.checked(n.exit_code(info.process,c.byref(code_out)))
        drain(job)
    finally:
        if info.process and n.wait_object(info.process,0) == 0x102:
            drain(job)
        for handle in (info.thread,info.process):
            if handle:
                n.checked(n.close_handle(handle))
        for stream in logs:
            stream.close()
        delete_attrs(buffer)
    stdout = (folder / "stdout.bin").read_text(encoding="utf-8")
    stderr = (folder / "stderr.bin").read_text(encoding="utf-8")
    if code_out.value != 0:
        raise RuntimeError({"exit_code":code_out.value,"stdout":stdout,"stderr":stderr})
    return json.loads(stdout)

CHILD = r'''
from pathlib import Path
import json, os, subprocess, sys
root = Path(sys.argv[1])
result = {"absolute_read": (root/"identity.txt").read_text(encoding="utf-8")}
(root/"main-write.txt").write_text("MAIN",encoding="utf-8")
extended = Path("\\\\?\\" + str(root))/"identity.txt"
result["extended_path_read"] = extended.read_text(encoding="utf-8")
grandchild = "from pathlib import Path; import json,sys; r=Path(sys.argv[1]); (r/'child-write.txt').write_text('CHILD',encoding='utf-8'); print(json.dumps({'read':(r/'identity.txt').read_text(encoding='utf-8')}))"
child = subprocess.run([sys.executable,"-c",grandchild,str(root)],capture_output=True,text=True,encoding="utf-8",timeout=10,check=True)
result["grandchild"] = json.loads(child.stdout)
print(json.dumps(result))
'''

def mapped_probe(folder, logical, backing):
    job = make_silo()
    try:
        try:
            apply_binding(job,logical,backing)
        except BindingDenied as error:
            return {"status":"blocked","stage":"scoped_binding","hresult":f"0x{error.hresult:08x}","error":str(error),"task_started":False}
        result = launch(job,folder,CHILD,logical)
        checks = {
            "absolute_path_redirected": result["absolute_read"] == "VIEW",
            "extended_path_redirected": result["extended_path_read"] == "VIEW",
            "grandchild_redirected": result["grandchild"]["read"] == "VIEW",
            "writes_in_backing": (backing/"main-write.txt").read_text(encoding="utf-8") == "MAIN" and (backing/"child-write.txt").read_text(encoding="utf-8") == "CHILD",
            "host_identity_unchanged": (logical/"identity.txt").read_text(encoding="utf-8") == "HOST",
            "host_not_written": not (logical/"main-write.txt").exists() and not (logical/"child-write.txt").exists(),
        }
        return {"status":"passed" if all(checks.values()) else "failed","checks":checks,"child":result}
    finally:
        job.close()

def execute(report):
    scratch = Path(tempfile.mkdtemp(prefix="fixture-",dir=Path(__file__).resolve().parent)).resolve()
    assert scratch.parent == Path(__file__).resolve().parent
    report["fixture_directory"] = str(scratch)
    job = make_silo()
    try:
        report["silo_launch"] = launch(job,scratch,"import json; print(json.dumps({'started':True}))")
    finally:
        job.close()
    logical, backing = scratch/"logical",scratch/"backing"
    logical.mkdir()
    backing.mkdir()
    (logical/"identity.txt").write_text("HOST",encoding="utf-8")
    (backing/"identity.txt").write_text("VIEW",encoding="utf-8")
    report["ordinary_directory_mapping"] = mapped_probe(scratch,logical,backing)
    executable = native_executable()
    report["native_executable_sha256"] = hashlib.file_digest(executable.open("rb"),"sha256").hexdigest()
    native_root = scratch/"native-project"
    native_root.mkdir()
    (native_root/"identity.txt").write_text("HOST",encoding="utf-8")
    client = Client(executable,scratch/"native-store",native_root)
    try:
        started = client.begin("silo-probe")
        if started["status"] != "active":
            raise RuntimeError(started)
        mount = Path(started["mount"])
        (mount/"identity.txt").write_text("VIEW",encoding="utf-8")
        report["winfsp_mount_accessible"] = (mount/"identity.txt").read_text(encoding="utf-8") == "VIEW"
        report["winfsp_mapping"] = mapped_probe(scratch,native_root,mount)
        sealed = client.request("finish")
        if sealed["status"] != "sealed":
            raise RuntimeError(sealed)
        report["native_recorded_paths"] = [item["path"] for item in sealed["receipt"]["changes"]]
        report["native_image_fields"] = sorted(sealed["receipt"]["changes"][0]["after"])
        discarded = client.request("discard",command_id="silo-probe")
        report["discard_result"] = discarded
    finally:
        client.close()
    report["mount_released"] = not mount.exists()
    report["host_native_identity_unchanged"] = (native_root/"identity.txt").read_text(encoding="utf-8") == "HOST"
    report["publication_and_restore_tested"] = False
    report["aliases_and_inherited_host_handles_tested"] = False

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output",required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    assert output.parent == Path(__file__).resolve().parent
    report = {
        "windows_build":sys.getwindowsversion().build,
        "is_admin":bool(c.windll.shell32.IsUserAnAdmin()),
        "bind_filter_export_present":True,
        "scope":"temporary Silo-scoped mappings only; no global bindings or system configuration changes",
    }
    try:
        execute(report)
    except BaseException as error:
        report["experiment_error"] = {"type":type(error).__name__,"message":str(error),"traceback":traceback.format_exc()}
        raise
    finally:
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__ == "__main__":
    main()

