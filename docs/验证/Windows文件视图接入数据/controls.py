"""Focused control cases; all mappings use a non-null Silo job."""
import ctypes as c
from ctypes import wintypes as w
import json
from pathlib import Path
import subprocess
import sys
import probe

root = Path(__file__).resolve().parent
report = {"is_admin":bool(c.windll.shell32.IsUserAnAdmin()), "results":{}}
def attempt(label, logical, backing):
    job = probe.make_silo()
    try:
        hr = probe.setup(job._handle,4,str(logical),str(backing),None,0)
        item = {"hresult":f"0x{hr & 0xffffffff:08x}","message":c.FormatError(hr).strip() if hr < 0 else "success"}
        if hr >= 0:
            folder = root/"control-fixtures"/label
            logical_path = folder/"logical"
            child = probe.launch(job,folder,
                "import json,sys; from pathlib import Path; p=Path(sys.argv[1]); print(json.dumps({'read':(p/'identity.txt').read_text(encoding='utf-8')}))",
                logical_path)
            item["child"] = child
            item["host_visible"] = (logical_path/"identity.txt").read_text(encoding="utf-8") if logical_path.exists() else "absent"
        report["results"][label] = item
    finally:
        job.close()

try:
    for label in ("existing","absent","ntpaths"):
        folder = root/"control-fixtures"/label
        logical, target = str(folder/"logical"),str(folder/"backing")
        if label == "ntpaths":
            logical,target = "\\??\\"+logical,"\\??\\"+target
        attempt(label,logical,target)

    adv = c.WinDLL("advapi32",use_last_error=True)
    open_token = adv.OpenProcessToken
    open_token.argtypes = [w.HANDLE,w.DWORD,c.POINTER(w.HANDLE)]
    open_token.restype = w.BOOL
    lookup = adv.LookupPrivilegeValueW
    class Luid(c.Structure):
        _fields_ = [("low",w.DWORD),("high",w.LONG)]
    class Entry(c.Structure):
        _fields_ = [("luid",Luid),("attributes",w.DWORD)]
    class Privileges(c.Structure):
        _fields_ = [("count",w.DWORD),("entries",Entry*1)]
    lookup.argtypes = [w.LPCWSTR,w.LPCWSTR,c.POINTER(Luid)]
    lookup.restype = w.BOOL
    adjust = adv.AdjustTokenPrivileges
    adjust.argtypes = [w.HANDLE,w.BOOL,c.POINTER(Privileges),w.DWORD,c.c_void_p,c.c_void_p]
    adjust.restype = w.BOOL
    current_process = probe.n.api("GetCurrentProcess",[],w.HANDLE)
    token = w.HANDLE()
    probe.n.checked(open_token(current_process(),0x20|0x8,c.byref(token)))
    report["temporary_token_privileges"] = {}
    try:
        for name in ("SeBackupPrivilege","SeRestorePrivilege"):
            privileges = Privileges()
            privileges.count = 1
            probe.n.checked(lookup(None,name,c.byref(privileges.entries[0].luid)))
            privileges.entries[0].attributes = 2
            c.set_last_error(0)
            probe.n.checked(adjust(token,False,c.byref(privileges),0,None,None))
            report["temporary_token_privileges"][name] = c.get_last_error()
    finally:
        probe.n.checked(probe.n.close_handle(token))
    folder = root/"control-fixtures"/"after-privileges"
    attempt("after-privileges",folder/"logical",folder/"backing")
except BaseException as error:
    report["error"] = {"type":type(error).__name__,"message":str(error)}
    raise
finally:
    (root/"controls.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")

