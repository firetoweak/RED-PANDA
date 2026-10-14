"""Small exploratory timing, excluding process startup from body measurements."""
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
sys.path.insert(0,str(Path.cwd()))
from redpanda.filesystem.file_view.client import Client,native_executable
root=Path(__file__).resolve().parent
fixture=Path(tempfile.mkdtemp(prefix="timing-",dir=root));host=fixture/"host";host.mkdir();outside=fixture/"outside";outside.mkdir()
code="from pathlib import Path;import time,json; p=Path('work');p.mkdir();data=b'x'*4096;t=time.perf_counter();\nfor i in range(200):\n f=p/str(i);f.write_bytes(data);assert f.read_bytes()==data\nprint(json.dumps({'body_ms':(time.perf_counter()-t)*1000}))"
rows=[]
def measure(kind,hooked,cwd,iteration):
    cwd.mkdir(exist_ok=True)
    argv=[sys._base_executable,"-c",code]
    if hooked:argv=[str(root/"launcher.exe"),str(host),str(root/"hook.dll"),str(cwd),*argv]
    start=time.perf_counter();p=subprocess.run(argv,cwd=cwd,capture_output=True,timeout=30,check=True)
    row={"filesystem":kind,"hooked":hooked,"iteration":iteration,"wall_ms":(time.perf_counter()-start)*1000,**json.loads(p.stdout)}
    rows.append(row)
with Client(native_executable(),fixture/"store",host) as client:
    for iteration in range(4):
        # Alternate order to avoid consistently favoring one variant's warm state.
        for hooked in ([False,True] if iteration%2==0 else [True,False]):
            measure("ntfs",hooked,outside/f"{iteration}-{hooked}",iteration)
            identity=f"{iteration}-{hooked}";mount=Path(client.begin(identity)["mount"])
            try:measure("winfsp",hooked,mount,iteration)
            finally:client.request("finish");client.request("discard",command_id=identity)
summary={}
for kind in ("ntfs","winfsp"):
    groups={hooked:statistics.median(r["body_ms"] for r in rows if r["filesystem"]==kind and r["hooked"]==hooked) for hooked in (False,True)}
    summary[kind]={"unhooked_median_ms":groups[False],"hooked_median_ms":groups[True],"body_ratio":groups[True]/groups[False]}
record={"workload":"200 new files; each write 4096 bytes then read; body excludes mkdir and process startup; four repetitions per variant","summary":summary,"samples":rows}
(root/"benchmark.json").write_text(json.dumps(record,indent=2),encoding="utf-8")
print(json.dumps(summary),flush=True)
