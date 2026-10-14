"""Installed task runtime works with no compiler on PATH."""
import asyncio
import os
from pathlib import Path
import shutil

import pytest

from scripts import execution_runtime
from redpanda.execution.local import windows_task
from redpanda.execution.constraint import TaskWriteConstraint
from redpanda.execution.local.powershell import PowerShellCommandRunner

pytestmark=[pytest.mark.process,pytest.mark.skipif(os.name!="nt",reason="Windows runtime")]


def test_packaged_execution_runtime_enforces_host_read_only_without_toolchain(tmp_path,monkeypatch):
    root=Path(__file__).resolve().parents[2]
    archive=root/"dist/agentFS/redpanda-execution-windows-x86_64.zip"
    assert archive.is_file(),"Build and package the Windows task runtime before process verification"
    config=tmp_path/"native/filesystem/release.json";config.parent.mkdir(parents=True)
    shutil.copyfile(root/"native/filesystem/release.json",config)
    monkeypatch.setattr(execution_runtime.delivery,"download",lambda url,target:shutil.copyfile(archive.with_name(url.rsplit("/",1)[1]),target))
    launcher=execution_runtime.install(tmp_path)
    monkeypatch.setattr(windows_task,"native_task_launcher",lambda:launcher)
    async def scenario():
        workspace=tmp_path/"project";workspace.mkdir();(workspace/"file").write_text("BASE")
        environment=dict(os.environ);environment["PATH"]=str(Path(os.environ["SystemRoot"])/"System32")
        constraint=TaskWriteConstraint(workspace)
        runner=PowerShellCommandRunner(executable=str(Path(os.environ["SystemRoot"])/"System32/WindowsPowerShell/v1.0/powershell.exe"),environment=environment,write_constraint=constraint)
        result=await runner.run("try {[IO.File]::WriteAllText('file','BAD');exit 9} catch [UnauthorizedAccessException] {Write-Output DENIED}",constraint.for_command(workspace,may_write=False),20)
        assert result.exit_code==0,result
        assert "DENIED" in result.stdout.content
        assert (workspace/"file").read_text()=="BASE"
    asyncio.run(scenario())
