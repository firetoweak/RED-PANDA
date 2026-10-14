"""BuildXL initialization and token-privilege controls, no global bindings."""
import ctypes as c
from ctypes import wintypes as w
import json
from pathlib import Path
import tempfile
import traceback
import probe

root = Path(__file__).resolve().parent
report = {"is_admin": bool(c.windll.shell32.IsUserAnAdmin()), "privileges": {}, "results": {}}
adjust = c.WinDLL("ntdll").RtlAdjustPrivilege
adjust.argtypes = [w.ULONG, c.c_ubyte, c.c_ubyte, c.POINTER(c.c_ubyte)]
adjust.restype = w.LONG
container = c.WinDLL("container.dll")
describe = container.WcCreateDescriptionFromXml
describe.argtypes = [w.LPCWSTR, c.POINTER(c.c_void_p)]
describe.restype = w.LONG
create = container.WcCreateContainer
create.argtypes = [w.HANDLE, c.c_void_p, w.BOOL]
create.restype = w.LONG
cleanup = container.WcCleanupContainer
cleanup.argtypes = [w.HANDLE, w.LPCWSTR]
cleanup.restype = w.LONG
destroy = container.WcDestroyDescription
destroy.argtypes = [c.c_void_p]
destroy.restype = None
enabled = []

def hr(result):
    return {"hresult": f"0x{result & 0xffffffff:08x}", "message": c.FormatError(result).strip() if result < 0 else "success"}

def fixture(label):
    folder = Path(tempfile.mkdtemp(prefix=label+"-",dir=root))
    for name, marker in (("logical", "HOST"),("backing", "VIEW")):
        (folder/name).mkdir()
        (folder/name/"identity.txt").write_text(marker,encoding="utf-8")
    return folder

def container_probe(folder, logical, backing):
    job = probe.WindowsJob.create()
    description = c.c_void_p()
    created = False
    item = {}
    try:
        result = describe("<container></container>", c.byref(description))
        item["description"] = hr(result)
        if result < 0:
            return item
        result = create(job._handle,description,False)
        item["container_creation"] = hr(result)
        if result < 0:
            return item
        created = True
        result = probe.setup(job._handle,4,str(logical),str(backing),None,0)
        item["binding"] = hr(result)
        item["task_started"] = result >= 0
        if result >= 0:
            item["child"] = probe.launch(job,folder,probe.CHILD,logical)
            item["host_identity"] = (logical/"identity.txt").read_text(encoding="utf-8")
            item["host_not_written"] = not (logical/"main-write.txt").exists() and not (logical/"child-write.txt").exists()
    finally:
        if created:
            result = cleanup(job._handle,None)
            item["cleanup"] = hr(result)
            if result < 0:
                raise RuntimeError(item["cleanup"])
        if description:
            destroy(description)
        job.close()
    return item

try:
    for name, number in (("SeLoadDriverPrivilege",10),("SeManageVolumePrivilege",28)):
        previous = c.c_ubyte()
        result = adjust(number,True,False,c.byref(previous))
        report["privileges"][name] = {"ntstatus":f"0x{result & 0xffffffff:08x}","previous_enabled": bool(previous.value)}
        if result < 0:
            raise RuntimeError(report["privileges"][name])
        enabled.append((number,previous.value))
        folder = fixture(name)
        report["results"][name] = probe.mapped_probe(folder,folder/"logical",folder/"backing")
    folder = fixture("container")
    report["results"]["container_ordinary"] = container_probe(folder,folder/"logical",folder/"backing")
    # Run the original WinFsp test with both privileges enabled.
    report["with_privileges"] = {}
    probe.execute(report["with_privileges"])
    folder = fixture("narrow-winfsp")
    client = probe.Client(probe.native_executable(),folder/"store",folder/"logical")
    try:
        started = client.begin("narrow-winfsp")
        if started["status"] != "active":
            raise RuntimeError(started)
        mount = Path(started["mount"])
        (mount/"sub").mkdir()
        (mount/"sub"/"identity.txt").write_text("VIEW",encoding="utf-8")
        report["results"]["winfsp_subdirectory"] = probe.mapped_probe(folder,folder/"logical",mount/"sub")
        report["results"]["container_winfsp_subdirectory"] = container_probe(folder,folder/"logical",mount/"sub")
        sealed = client.request("finish")
        if sealed["status"] != "sealed":
            raise RuntimeError(sealed)
        discarded = client.request("discard",command_id="narrow-winfsp")
        if discarded["status"] != "discarded":
            raise RuntimeError(discarded)
        report["narrow_discarded"] = True
    finally:
        client.close()
    report["narrow_mount_released"] = not mount.exists()
    report["narrow_host_identity"] = (folder/"logical"/"identity.txt").read_text(encoding="utf-8")
except BaseException as error:
    report["error"] = {"type":type(error).__name__,"message":str(error),"traceback":traceback.format_exc()}
    raise
finally:
    for number, previous in reversed(enabled):
        ignored = c.c_ubyte()
        result = adjust(number,previous,False,c.byref(ignored))
        report.setdefault("privilege_restore",{})[str(number)] = f"0x{result & 0xffffffff:08x}"
    (root/"prerequisites.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
