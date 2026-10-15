"""Optional local CLI verification through production tools; artifacts stay in .tools."""
import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from redpanda.assistant.builtin_tools import build_builtin_tools
from redpanda.filesystem.operations import WorkspaceFiles,operation_id
from tests.fixtures.workspaces import workspace_record


def ps(*argv):
    return "& "+" ".join("'"+str(item).replace("'","''")+"'" for item in argv)


async def main():
    artifacts=ROOT/".tools/windows-execution-tools";artifacts.mkdir(exist_ok=True)
    fixture=Path(tempfile.mkdtemp(prefix="run-",dir=artifacts));root=fixture/"project";root.mkdir()
    (root/"value").write_text("BASE")
    (root/"program.cpp").write_text("int main() { return 0; }\n")
    git=shutil.which("git");node=shutil.which("node")
    assert git and node,"Git and Node must be available for this optional verification"
    subprocess.run([git,"init","-q",str(root)],check=True)
    files=WorkspaceFiles(root,fixture/"store")
    tools=await build_builtin_tools(workspace_record(root),filesystem=files,session_operations=lambda:asyncio.sleep(0,result=()))
    records=[]
    async def run(name,command,readonly=False):
        invoke=lambda:tools.execute("execute_command",{"command":command,"workspace_effect":"read_only" if readonly else "may_write","timeout_seconds":60})
        result=await files.read(invoke) if readonly else await files.execute(operation_id("cli-check",name),invoke)
        records.append({"name":name,"result":result})
        (artifacts/"results.json").write_text(json.dumps({"fixture":str(fixture),"commands":records},ensure_ascii=False,indent=2),encoding="utf-8")
        assert result["code"]=="COMMAND_COMPLETED" and result["data"]["exit_code"]==0,result
    js="const f=require('fs');f.writeFileSync('node.txt','NODE');if(f.readFileSync('node.txt','utf8')!=='NODE')process.exit(2);try{f.writeFileSync(process.argv[1],'BAD');process.exit(3)}catch(e){if(e.code!=='EPERM'&&e.code!=='EACCES')throw e}"
    await run("node",ps(node,"-e",js,root/"value"))
    compiler=ROOT/".tools/llvm-mingw-20261006-ucrt-x86_64/bin/x86_64-w64-mingw32-clang++.exe"
    await run("clang",ps(compiler,"program.cpp","-o","hello.exe")+"; if ($LASTEXITCODE) {exit $LASTEXITCODE}; & .\\hello.exe; exit $LASTEXITCODE")
    await run("git-add",ps(git,"add","node.txt","program.cpp"))
    await run("git-diff",ps(git,"diff","--cached","--stat"),True)
    assert (root/"value").read_text()=="BASE"
    assert (root/"node.txt").read_text()=="NODE"
    assert (root/"hello.exe").is_file()
    print(json.dumps({"commands":len(records),"fixture":str(fixture),"host_direct_write_denied":True}))


if __name__=="__main__":
    asyncio.run(main())
