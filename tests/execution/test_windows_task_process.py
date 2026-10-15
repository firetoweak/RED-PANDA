"""Real Windows task restrictions through the production tool composition."""
import asyncio
import ctypes
import json
import os
from pathlib import Path
import shutil
import sys

import pytest

from redpanda.assistant.builtin_tools import build_builtin_tools
from redpanda.filesystem.operations import WorkspaceFiles, operation_id
from redpanda.tools.builtin.command_interrupts import run_interruptible
from redpanda.filesystem.file_view.client import native_executable
from redpanda.execution.local import windows_task
from tests.fixtures.workspaces import workspace_record

pytestmark = [pytest.mark.process, pytest.mark.skipif(os.name != "nt", reason="Windows execution")]


def ps(*argv):
    return "& " + " ".join("'" + str(value).replace("'", "''") + "'" for value in argv)


async def tools_at(root, storage):
    view = WorkspaceFiles(root, storage)
    tools = await build_builtin_tools(workspace_record(root), filesystem=view, session_operations=lambda: asyncio.sleep(0, result=()))
    return view, tools


def test_readonly_attribute_is_tracked_through_hook_and_host_clear_is_denied(tmp_path):
    async def scenario():
        root=tmp_path/"project";root.mkdir();path=root/"file";path.write_text("bytes")
        view,tools=await tools_at(root,tmp_path/"store")
        try:
            identity=operation_id("test","readonly")
            result=await view.execute(identity,lambda:tools.execute("execute_command",{
                "command":"[IO.File]::SetAttributes((Join-Path $PWD.Path 'file'),[IO.FileAttributes]::ReadOnly); (Get-Item file).IsReadOnly",
            }))
            assert result["data"]["exit_code"]==0,result
            assert "True" in result["data"]["stdout"]["content"]
            assert path.stat().st_file_attributes&1
            receipt=json.loads((view.storage/"commands"/identity/"sealed.json").read_text())
            assert receipt["changes"][0]["after"]["readonly"] is True
            result=await view.read(lambda:tools.execute("execute_command",{
                "workspace_effect":"read_only",
                "command":"try {[IO.File]::SetAttributes((Join-Path $PWD.Path 'file'),[IO.FileAttributes]::Normal);exit 9} catch [UnauthorizedAccessException] {Write-Output DENIED}",
            }))
            assert result["data"]["exit_code"]==0 and "DENIED" in result["data"]["stdout"]["content"],result
            assert path.stat().st_file_attributes&1
        finally:
            path.chmod(0o666)
    asyncio.run(scenario())


def test_default_git_init_publishes_hidden_git_directory_through_task_hook(tmp_path):
    git = shutil.which("git")
    assert git is not None, "Git is required for the Windows coding process contract"
    async def scenario():
        root = tmp_path / "project"; root.mkdir()
        (root / "file.txt").write_text("tracked bytes")
        view = WorkspaceFiles(root, tmp_path / "store")
        tools = await build_builtin_tools(
            workspace_record(root), filesystem=view,
            command_environment={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
            session_operations=lambda: asyncio.sleep(0, result=()),
        )
        identity = operation_id("test", "git-init")
        command = f"{ps(git, 'init')}; if ($LASTEXITCODE) {{exit $LASTEXITCODE}}; {ps(git, 'add', 'file.txt')}; if ($LASTEXITCODE) {{exit $LASTEXITCODE}}; {ps(git, 'diff', '--cached', '--name-only')}"
        result = await view.execute(identity, lambda: tools.execute("execute_command", {"command": command}))
        assert result["data"]["exit_code"] == 0, result
        assert "file.txt" in result["data"]["stdout"]["content"]
        assert (root / ".git").stat().st_file_attributes & 2
        receipt = json.loads((view.storage / "commands" / identity / "sealed.json").read_text())
        assert next(item for item in receipt["changes"] if item["path"] == "/.git")["after"]["hidden"] is True
    asyncio.run(scenario())


def test_read_only_task_can_read_but_cannot_modify_host(tmp_path):
    async def scenario():
        root = tmp_path / "项目"; root.mkdir(); (root / "value").write_text("BASE")
        view, tools = await tools_at(root, tmp_path / "store")
        result = await view.read(lambda: tools.execute("execute_command", {
            "workspace_effect": "read_only",
            "command": "Get-Content value; try {[IO.File]::WriteAllText('value','BAD');exit 9} catch [UnauthorizedAccessException] {Write-Output DENIED}",
        }))
        assert result["data"]["exit_code"] == 0, result
        assert "DENIED" in result["data"]["stdout"]["content"]
        assert result["data"]["command_cwd"] == str(root)
        assert (root / "value").read_text() == "BASE"
        assert not (view.storage / "commands").exists()
    asyncio.run(scenario())


def test_nonfile_device_has_no_filesystem_parent_to_check(tmp_path):
    async def scenario():
        root=tmp_path/"project";root.mkdir()
        view,tools=await tools_at(root,tmp_path/"store")
        result=await view.read(lambda:tools.execute("execute_command",{
            "workspace_effect":"read_only","command":ps(sys._base_executable,"-c","open('nul','w').write('discard')"),
        }))
        assert result["data"]["exit_code"]==0,result
        assert not list(root.iterdir())
    asyncio.run(scenario())


def test_writable_task_child_reads_own_changes_and_rejects_host_write(tmp_path):
    assert native_executable().is_file(), "Build the file service before process verification"
    async def scenario():
        root = tmp_path / "project"; root.mkdir(); (root / "value").write_text("BASE")
        script = tmp_path / "child.py"
        script.write_text(
            "from pathlib import Path\nimport sys,subprocess,json,os\n"
            "host=Path(sys.argv[1]);Path('value').write_text('VIEW')\n"
            "assert Path('value').read_text()=='VIEW'\nassert (host/'value').read_text()=='BASE'\n"
            "try: (host/'value').write_text('BAD')\nexcept PermissionError: pass\nelse: raise AssertionError('host write')\n"
            "subprocess.run([sys.executable,'-c',\"from pathlib import Path;assert Path('value').read_text()=='VIEW';Path('child').write_text('CHILD')\"],env={'SystemRoot':os.environ['SystemRoot']},check=True)\n"
            "print(json.dumps({'cwd':str(Path.cwd())}))\n", encoding="utf-8")
        view, tools = await tools_at(root, tmp_path / "store")
        identity = operation_id("test", "write")
        result = await view.execute(identity, lambda: tools.execute("execute_command", {"command": ps(sys.executable, "-B", script, root)}))
        assert result["data"]["exit_code"] == 0, result
        assert result["data"]["command_cwd"] != str(root)
        assert (root / "value").read_text() == "VIEW"
        assert (root / "child").read_text() == "CHILD"
        sealed = json.loads((view.storage / "commands" / identity / "sealed.json").read_text())
        assert {item["path"] for item in sealed["changes"]} == {"/value", "/child"}
    asyncio.run(scenario())


def test_missing_execution_component_prevents_user_command(tmp_path, monkeypatch):
    fake = tmp_path / "runtime/redpanda-command.exe"
    fake.parent.mkdir(); shutil.copyfile(windows_task.native_task_launcher(), fake)
    monkeypatch.setattr(windows_task, "native_task_launcher", lambda: fake)
    async def scenario():
        root = tmp_path / "project"; root.mkdir(); marker = root / "ran"
        view, tools = await tools_at(root, tmp_path / "store")
        result = await view.read(lambda: tools.execute("execute_command", {"workspace_effect": "read_only", "command": "Set-Content ran BAD"}))
        assert result["code"] == "COMMAND_START_FAILED"
        assert not marker.exists()
    asyncio.run(scenario())


def test_invalid_dll_is_not_replaced_by_an_unhooked_launch(tmp_path,monkeypatch):
    directory=tmp_path/"runtime";directory.mkdir()
    launcher=directory/"redpanda-command.exe"
    shutil.copyfile(windows_task.native_task_launcher(),launcher)
    (directory/"redpanda-hook.dll").write_bytes(b"invalid image")
    monkeypatch.setattr(windows_task,"native_task_launcher",lambda:launcher)
    async def scenario():
        root=tmp_path/"project";root.mkdir();marker=tmp_path/"ran"
        view,tools=await tools_at(root,tmp_path/"store")
        result=await view.read(lambda:tools.execute("execute_command",{"workspace_effect":"read_only","command":f"Set-Content -LiteralPath '{marker}' BAD"}))
        assert result["code"]=="COMMAND_START_FAILED",result
        assert not marker.exists()
    asyncio.run(scenario())


def test_child_dll_loss_stops_task_before_child_entry(tmp_path,monkeypatch):
    directory=tmp_path/"runtime";directory.mkdir()
    original=windows_task.native_task_launcher()
    for name in ("redpanda-command.exe","redpanda-hook.dll"):
        shutil.copyfile(original.with_name(name),directory/name)
    monkeypatch.setattr(windows_task,"native_task_launcher",lambda:directory/"redpanda-command.exe")
    async def scenario():
        root=tmp_path/"project";root.mkdir();marker=tmp_path/"child-ran"
        dll=directory/"redpanda-hook.dll"
        child_code=f"open({str(marker)!r},'w').write('BAD')"
        code=f"from pathlib import Path;import subprocess,sys;Path({str(dll)!r}).rename({str(dll.with_suffix('.held'))!r});subprocess.run([sys.executable,'-c',{child_code!r}],check=True)"
        view,tools=await tools_at(root,tmp_path/"store")
        result=await view.read(lambda:tools.execute("execute_command",{"workspace_effect":"read_only","command":ps(sys._base_executable,"-c",code)}))
        assert result["code"]=="COMMAND_CONSTRAINT_FAILED",result
        assert not marker.exists()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind",["hardlink","junction","extended"])
def test_host_alias_writes_are_rejected(tmp_path,kind):
    async def scenario():
        root=tmp_path/"project";root.mkdir();file=root/"value";file.write_text("BASE")
        if kind=="hardlink":
            alias=tmp_path/"alias";os.link(file,alias)
        elif kind=="junction":
            junction=tmp_path/"junction"
            import subprocess
            subprocess.run([os.environ["COMSPEC"],"/d","/c","mklink","/J",str(junction),str(root)],capture_output=True,check=True)
            alias=junction/"value"
        else:
            alias=Path("\\\\?\\"+str(file))
        code=f"from pathlib import Path\ntry: Path({str(alias)!r}).write_text('BAD')\nexcept PermissionError: pass\nelse: raise AssertionError('alias write')"
        view,tools=await tools_at(root,tmp_path/"store")
        result=await view.read(lambda:tools.execute("execute_command",{"workspace_effect":"read_only","command":ps(sys._base_executable,"-c",code)}))
        assert result["data"]["exit_code"]==0,result
        assert file.read_text()=="BASE"
    asyncio.run(scenario())


def test_x86_child_is_stopped_and_constraint_failure_is_not_an_exit_code(tmp_path):
    async def scenario():
        root = tmp_path / "project"; root.mkdir()
        marker = tmp_path / "child-ran"
        child = Path(os.environ["SystemRoot"]) / "SysWOW64/cmd.exe"
        view, tools = await tools_at(root, tmp_path / "store")
        result = await view.read(lambda: tools.execute("execute_command", {
            "workspace_effect": "read_only", "command": ps(child, "/d", "/c", f'echo BAD > "{marker}"'),
        }))
        assert result["code"] == "COMMAND_CONSTRAINT_FAILED", result
        assert result["ok"] is None and result["data"]["exit_code"] is None
        assert "x64" in result["error"]
        assert not marker.exists()
    asyncio.run(scenario())


@pytest.mark.parametrize("entry,count", [("NtCreateUserProcess",11),("NtCreateProcess",8),("NtCreateProcessEx",9)])
def test_unmanaged_native_process_entry_is_rejected(tmp_path,entry,count):
    async def scenario():
        root=tmp_path/"project";root.mkdir()
        view,tools=await tools_at(root,tmp_path/"store")
        code=f"import ctypes;ctypes.WinDLL('ntdll').{entry}({','.join(['None']*count)})"
        result=await view.read(lambda:tools.execute("execute_command",{"workspace_effect":"read_only","command":ps(sys.executable,"-c",code)}))
        assert result["code"]=="COMMAND_CONSTRAINT_FAILED",result
    asyncio.run(scenario())


@pytest.mark.parametrize("directory_name",["runtime é space","命令组件 空格"])
def test_unicode_runtime_directory_preserves_task_path(tmp_path, monkeypatch,directory_name):
    directory = tmp_path / directory_name; directory.mkdir()
    for name in ("redpanda-command.exe", "redpanda-hook.dll"):
        shutil.copyfile(windows_task.native_task_launcher().with_name(name), directory / name)
    monkeypatch.setattr(windows_task, "native_task_launcher", lambda: directory / "redpanda-command.exe")
    async def scenario():
        root = tmp_path / "任务"; root.mkdir()
        marker=tmp_path/"entered"
        view, tools = await tools_at(root, tmp_path / "store")
        result = await view.read(lambda: tools.execute("execute_command", {"workspace_effect": "read_only", "command": f"[IO.File]::WriteAllText('{marker}','YES'); Write-Output (Get-Location).Path"}))
        if result["code"]=="COMMAND_START_FAILED":
            # English Windows volumes may provide neither a lossless ANSI path
            # nor short names. That platform limitation must prevent user entry.
            def encodable(path):
                try: str(path).encode("mbcs")
                except UnicodeEncodeError: return False
                return True
            filename=directory/"redpanda-hook.dll"
            kernel=ctypes.WinDLL("kernel32",use_last_error=True)
            kernel.GetShortPathNameW.argtypes=[ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_ulong]
            buffer=ctypes.create_unicode_buffer(32768)
            count=kernel.GetShortPathNameW(str(filename),buffer,32768)
            assert not encodable(filename) and (not count or not encodable(buffer.value)),result
            assert not marker.exists()
            return
        assert result["data"]["exit_code"] == 0, result
        assert result["data"]["stdout"]["content"].strip() == str(root)
        assert marker.exists()
    asyncio.run(scenario())


def test_interrupt_reaps_writer_before_file_operation_finishes(tmp_path,monkeypatch):
    from redpanda.filesystem.file_view.client import Client
    original_request=Client.request
    observed={}
    def checked_request(client,op,**fields):
        if op=="finish":
            assert observed["pid"]
            kernel=ctypes.WinDLL("kernel32",use_last_error=True)
            kernel.OpenProcess.argtypes=[ctypes.c_ulong,ctypes.c_int,ctypes.c_ulong];kernel.OpenProcess.restype=ctypes.c_void_p
            handle=kernel.OpenProcess(0x100000,False,observed["pid"])
            if handle:
                kernel.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_ulong]
                kernel.CloseHandle.argtypes=[ctypes.c_void_p]
                assert kernel.WaitForSingleObject(handle,0)==0,"file view finished before task exit"
                kernel.CloseHandle(handle)
            else:
                assert ctypes.get_last_error()==87
            observed["finished"]=True
        return original_request(client,op,**fields)
    monkeypatch.setattr(Client,"request",checked_request)
    async def scenario():
        root = tmp_path / "project"; root.mkdir()
        view, tools = await tools_at(root, tmp_path / "store")
        ready = tmp_path / "ready"
        prepared=ready.with_suffix(".tmp")
        code = f"from pathlib import Path;import time,os;Path({str(prepared)!r}).write_text(str(os.getpid()));os.replace({str(prepared)!r},{str(ready)!r});time.sleep(10);Path('late').write_text('BAD')"
        task = asyncio.create_task(run_interruptible(tools.command_interrupts, session_id="test",command_id="stop",attempt_id="stop",
            execute=lambda: view.execute(operation_id("test","stop"), lambda: tools.execute("execute_command", {"command": ps(sys.executable, "-u", "-c", code)}))))
        for _ in range(1000):
            if ready.exists():
                break
            await asyncio.sleep(0.01)
        assert ready.exists(), "task did not start"
        pid = int(ready.read_text())
        observed["pid"]=pid
        tools.command_interrupts.signal()
        result = await task
        assert result["code"] == "COMMAND_INTERRUPTED", result
        assert not (root / "late").exists()
        assert observed["finished"]
    asyncio.run(scenario())
