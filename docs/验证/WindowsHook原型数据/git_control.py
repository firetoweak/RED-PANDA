import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import ctypes
sys.path.insert(0,str(Path.cwd()))
from redpanda.filesystem.file_view.client import Client,native_executable
root=Path(__file__).resolve().parent
fixture=Path(tempfile.mkdtemp(prefix="git-control-",dir=root));host=fixture/"host";host.mkdir();outside=fixture/"outside";outside.mkdir()
git=shutil.which("git")
results=[]
def run(name,argv,cwd):
    p=subprocess.run(list(map(str,argv)),cwd=cwd,capture_output=True,timeout=30)
    result={"name":name,"returncode":p.returncode,"stdout":p.stdout.decode(errors="replace"),"stderr":p.stderr.decode(errors="replace")}
    results.append(result);print(json.dumps(result),flush=True)
    return p
p=run("hook-ntfs",[root/"launcher.exe",host,root/"hook.dll",outside,git,"init","-q"],outside)
assert p.returncode==0
with Client(native_executable(),fixture/"store",host) as client:
    mount=Path(client.begin("git")["mount"])
    try:
        p=run("unhooked-winfsp",[git,"init","-q"],mount)
        assert p.returncode==1 and b"Function not implemented" in p.stderr
        kernel=ctypes.WinDLL("kernel32",use_last_error=True)
        kernel.SetFileAttributesW.argtypes=[ctypes.c_wchar_p,ctypes.c_ulong]
        ok=kernel.SetFileAttributesW(str(mount/".git"),2|16)
        result={"name":"unhooked-hidden-attribute","success":bool(ok),"winerror":ctypes.get_last_error()}
        results.append(result);print(json.dumps(result))
        assert not ok and result["winerror"]==50
        p=run("unhooked-winfsp-hide-disabled",[git,"-c","core.hideDotFiles=false","init","-q"],mount)
        assert p.returncode==0
    finally:
        client.request("finish");client.request("discard",command_id="git")
(root/"git-control.json").write_text(json.dumps(results,indent=2),encoding="utf-8")
