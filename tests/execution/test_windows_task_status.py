import os
import struct

import pytest

from redpanda.execution.command import BoundedTextCapture, CaptureLimit, CommandConstraintError, CommandResult
from redpanda.execution.local import windows_task

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows startup protocol")


def protocol(tmp_path, monkeypatch, records):
    binary = tmp_path / "redpanda-command.exe"
    binary.touch();binary.with_name("redpanda-hook.dll").touch()
    monkeypatch.setattr(windows_task, "native_task_launcher", lambda: binary)
    task = windows_task.WindowsTask(tmp_path,tmp_path,["shell"])
    os.write(task.write_fd, b"".join(struct.pack("<IIIII",*record) for record in records))
    task.spawned()
    empty = BoundedTextCapture(CaptureLimit()).finish()
    return task, CommandResult(0,empty,empty,1,False)


def test_application_exit_125_is_not_a_startup_failure(tmp_path, monkeypatch):
    task,result=protocol(tmp_path,monkeypatch,[(1,1,0,0,0),(1,2,2,0,125)])
    assert task.finish(result).exit_code==125


def test_failure_after_start_preserves_output_as_partial_evidence(tmp_path, monkeypatch):
    task,result=protocol(tmp_path,monkeypatch,[(1,1,0,0,0),(1,3,3,50,0)])
    with pytest.raises(CommandConstraintError) as error:
        task.finish(result)
    assert error.value.result is result


@pytest.mark.parametrize("records",[[],[(1,1,0,0,0)],[(2,2,0,0,0)],[(1,3,99,5,0)],[(1,1,0,0,0),(1,3,2,5,0)]])
def test_invalid_status_is_an_internal_failure(tmp_path,monkeypatch,records):
    task,result=protocol(tmp_path,monkeypatch,records)
    with pytest.raises(RuntimeError):
        task.finish(result)
