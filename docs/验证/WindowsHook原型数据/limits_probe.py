"""Regression for the alias gap observed in the initial prototype."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

root=Path(__file__).resolve().parent
fixture=Path(tempfile.mkdtemp(prefix="limits-",dir=root))
host=fixture/"host";host.mkdir();file=host/"base.txt";file.write_text("BASE")
outside=fixture/"outside";outside.mkdir();alias=outside/"alias.txt"
os.link(file,alias)
code=f"from pathlib import Path;\nfor name in [{str(alias)!r},{str(alias)+':stream'!r}]:\n try: Path(name).write_text('ALIAS')\n except PermissionError: pass\n else: raise AssertionError('hardlink alias write permitted')"
p=subprocess.run([str(root/"launcher.exe"),str(host),str(root/"hook.dll"),str(outside),sys._base_executable,"-c",code],capture_output=True,timeout=30)
result={"name":"existing-outside-hardlink-and-stream","returncode":p.returncode,"stderr":p.stderr.decode(errors="replace"),"host_content":file.read_text(),"covered":file.read_text()=="BASE"}
print(json.dumps(result))
assert p.returncode==0 and result["covered"]
(root/"limits.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
