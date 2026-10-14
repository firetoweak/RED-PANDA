"""Standalone exploratory harness; not a product or pytest integration."""
import ctypes
import json
import msvcrt
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
REPO = Path.cwd()
sys.path.insert(0,str(REPO))
from redpanda.filesystem.file_view.client import Client, native_executable

results=[]
fixture=Path(tempfile.mkdtemp(prefix="fixtures-",dir=ROOT))
host=fixture/"任务目录"; host.mkdir();(host/"base.txt").write_text("BASE");(host/"empty").mkdir()
sibling=host.with_name(host.name+"-other");sibling.mkdir()
outside=fixture/"outside";outside.mkdir()
python=Path(sys._base_executable)

def snapshot():
    return {str(p.relative_to(host)): p.read_bytes().hex() for p in host.rglob("*") if p.is_file()}

def run(name,argv,cwd,*,dll=None,launcher=None,expected=0,**options):
    before=snapshot();started=time.perf_counter()
    cmd=[str(launcher or ROOT/"launcher.exe"),str(host),str(dll or ROOT/"hook.dll"),str(cwd),*map(str,argv)]
    completed=subprocess.run(cmd,capture_output=True,timeout=45,**options)
    item={"name":name,"returncode":completed.returncode,"elapsed_ms":round((time.perf_counter()-started)*1000,2),
          "stdout":completed.stdout.decode("utf-8",errors="replace"),"stderr":completed.stderr.decode("utf-8",errors="replace"),
          "host_unchanged":snapshot()==before}
    results.append(item);print(json.dumps(item,ensure_ascii=True),flush=True)
    assert completed.returncode==expected,item
    assert item["host_unchanged"],item

def case(name,cwd,**kw):
    run(name,[python,ROOT/"cases.py",name,host,outside],cwd,**kw)

try:
    case("host-read",host)
    case("host-mutations",host)
    case("path-spellings",outside)
    case("native-relative",outside)
    case("suspended-child",outside)
    case("x86-child",outside)
    case("process-a",outside)
    junction=outside/"junction"
    created=subprocess.run([os.environ["COMSPEC"],"/d","/c","mklink","/J",str(junction),str(host)],capture_output=True,check=True)
    run("junction",[python,ROOT/"cases.py","junction",host,outside,junction],outside)
    run("venv-redirector",[sys.executable,ROOT/"cases.py","host-read",host,outside],host)
    with (host/"base.txt").open("r+b",buffering=0) as file:
        handle=msvcrt.get_osfhandle(file.fileno());os.set_handle_inheritable(handle,True)
        si=subprocess.STARTUPINFO();si.lpAttributeList={"handle_list":[handle]}
        run("inherited-write",[python,ROOT/"cases.py","inherited-write",host,outside,handle],outside,startupinfo=si)
        os.set_handle_inheritable(handle,False)
    marker=outside/"missing-ran.txt"
    run("missing-dll",[python,"-c",f"open({str(marker)!r},'w').write('BAD')"],outside,dll=ROOT/"missing.dll",expected=126)
    assert not marker.exists()
    for name,options in [("missing-policy",{"launcher":ROOT/"launcher-no-policy.exe"}),("attach-failure",{"dll":ROOT/"hook-attach-failure.dll"})]:
        marker=outside/(name+"-ran.txt")
        run(name,[python,"-c",f"open({str(marker)!r},'w').write('BAD')"],outside,expected=126,**options)
        assert not marker.exists()
    # Parent already has the DLL loaded, but a child attachment must fail if it disappears.
    code="import os,subprocess,sys; from pathlib import Path; d=Path(sys.argv[1]); moved=d.with_suffix('.held'); d.rename(moved);\ntry:\n subprocess.run([sys.executable,'-c',sys.argv[2]],check=True)\nexcept OSError as e:\n assert e.winerror==1114,e\nelse:\n raise AssertionError('child was released')\nfinally:\n moved.rename(d)"
    marker=outside/"missing-child-ran.txt"
    run("missing-child-dll",[python,"-c",code,ROOT/"hook.dll",f"open({str(marker)!r},'w').write('BAD')"],outside)
    assert not marker.exists()
    with Client(native_executable(),fixture/"store",host) as client:
        mount=Path(client.begin("hook-probe")["mount"])
        try:
            case("mount",mount)
            case("child",mount)
            case("grandchild",mount)
            case("orphan-child",mount)
            orphan=int((outside/"orphan-pid.txt").read_text())
            kernel=ctypes.WinDLL("kernel32",use_last_error=True)
            kernel.OpenProcess.restype=ctypes.c_void_p
            kernel.OpenProcess.argtypes=[ctypes.c_ulong,ctypes.c_int,ctypes.c_ulong]
            process=kernel.OpenProcess(0x1000|0x100000,False,orphan)
            if process:
                kernel.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_ulong]
                kernel.CloseHandle.argtypes=[ctypes.c_void_p]
                assert kernel.WaitForSingleObject(process,0)==0
                kernel.CloseHandle(process)
            else:
                assert ctypes.get_last_error()==87
            ps=shutil.which("powershell")
            run("powershell",[ps,"-NoLogo","-NoProfile","-NonInteractive","-Command", "Set-Content -LiteralPath 'ps.txt' -Value 'PS'; if ((Get-Content -Raw 'ps.txt').Trim() -ne 'PS') {exit 2}"],mount)
            run("powershell-host-deny",[ps,"-NoLogo","-NoProfile","-NonInteractive","-Command",f"try {{ [IO.File]::WriteAllText('{host / 'base.txt'}','BAD'); exit 2 }} catch [UnauthorizedAccessException] {{ exit 0 }}"],mount)
            pwsh=shutil.which("pwsh")
            run("powershell-7",[pwsh,"-NoLogo","-NoProfile","-NonInteractive","-Command",f"[IO.File]::WriteAllText('ps7.txt','PS7'); try {{ [IO.File]::WriteAllText('{host / 'base.txt'}','BAD'); exit 2 }} catch [UnauthorizedAccessException] {{ exit 0 }}"],mount)
            node=shutil.which("node")
            run("node",[node,"-e","const fs=require('fs');fs.writeFileSync('node.txt','NODE');if(fs.readFileSync('node.txt','utf8')!=='NODE')process.exit(2);try{fs.writeFileSync(process.argv[1],'BAD');process.exit(3)}catch(e){if(e.code!=='EPERM'&&e.code!=='EACCES')throw e}",host/"base.txt"],mount)
            git=shutil.which("git")
            run("git-init-default",[git,"init","-q"],mount,expected=1)
            assert "Function not implemented" in results[-1]["stderr"]
            run("git-init-hide-disabled",[git,"-c","core.hideDotFiles=false","init","-q"],mount)
            run("git-add",[git,"add","base.txt","child.txt","ps.txt","node.txt"],mount)
            run("git-diff",[git,"diff","--cached","--stat"],mount)
            compiler=REPO/".tools/llvm-mingw-20261006-ucrt-x86_64/bin/x86_64-w64-mingw32-clang++.exe"
            run("clang-build",[compiler,"-x","c++","-","-o","hello.exe"],mount,input=b"int main() { return 0; }\n")
            run("compiled-program",[mount/"hello.exe"],mount)
        finally:
            sealed=client.request("finish")
            (ROOT/"sealed.json").write_text(json.dumps(sealed,ensure_ascii=False,indent=2),encoding="utf-8")
            client.request("discard",command_id="hook-probe")
        assert sealed["status"]=="sealed"
        changes={change["path"]:change for change in sealed["receipt"]["changes"]}
        assert changes["/base.txt"]["before"]["size"]==4
        assert changes["/base.txt"]["after"]["size"]==4
        assert changes["/base.txt"]["before"]["blocks"]!=changes["/base.txt"]["after"]["blocks"]
        for name in ("/新文件.txt","/child.txt","/ps.txt","/node.txt","/.git/index"):
            assert changes[name]["before"]["kind"]=="missing",name
            assert changes[name]["after"]["kind"]=="file",name
    assert (host/"base.txt").read_text()=="BASE"
    assert sorted(p.name for p in host.iterdir())==["base.txt","empty"]
finally:
    record={"platform":platform.platform(),"python":sys.version,"fixture":str(fixture),"detours_commit":(ROOT/"DETOURS_COMMIT.txt").read_text().strip(),"results":results}
    (ROOT/"results.json").write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding="utf-8")
