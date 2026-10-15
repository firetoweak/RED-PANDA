"""A gate timeout remains a lifecycle fault when the target exits first."""
import os
from pathlib import Path
import subprocess

import pytest

from redpanda.execution.command import BoundedTextCapture, CaptureLimit, CommandResult
from redpanda.execution.local import windows_task

pytestmark = [pytest.mark.process, pytest.mark.skipif(os.name != "nt", reason="Windows gate")]


@pytest.fixture(scope="module")
def gate_runtime(tmp_path_factory):
    source_root = Path(__file__).resolve().parents[2]
    runtime_root = Path(windows_task.__file__).resolve().parents[3]
    compiler = runtime_root / ".tools/llvm-mingw-20261006-ucrt-x86_64/bin/x86_64-w64-mingw32-clang++.exe"
    library = runtime_root / ".tools/execution/detours.a"
    assert compiler.is_file() and library.is_file(), "Build Windows execution components before process verification"
    source = source_root / "native/execution"
    runtime = tmp_path_factory.mktemp("gate-runtime")
    common = [str(compiler), "-std=c++17", "-O2", "-static", "-DDETOUR_DEBUG=0", "-D_WIN32_WINNT=0x0A00",
              "-I", str(source), "-I", str(source / "vendor/detours/src")]
    subprocess.run([*common, "-shared", str(source_root / "tests/fixtures/windows_gate_hook.cpp"), str(source / "hook.def"),
                    str(library), "-o", str(runtime / "redpanda-hook.dll")], check=True, capture_output=True)
    launcher = runtime / "redpanda-command.exe"
    subprocess.run([*common, "-municode", str(source_root / "tests/fixtures/windows_gate_launcher.cpp"),
                    str(library), "-o", str(launcher)], check=True, capture_output=True)
    return launcher


@pytest.mark.parametrize("case", ["short-delay", "withhold", "late-go", "fault-exit-race", "access-denied-live", "job-child"])
def test_gate_timeout_is_not_an_application_exit(tmp_path, monkeypatch, gate_runtime, case):
    root = tmp_path / "workspace"
    root.mkdir()
    marker = tmp_path / "user-tls-entered"
    monkeypatch.setattr(windows_task, "native_task_launcher", lambda: gate_runtime)
    task = windows_task.WindowsTask(root, root, [str(gate_runtime), "--target"])
    environment = dict(os.environ, REDPANDA_GATE_CASE=case, REDPANDA_GATE_EXPECTED_MARKER=str(marker))
    process = subprocess.Popen(task.argv, cwd=root, env=environment, startupinfo=task.startupinfo,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    task.spawned()
    try:
        stdout, stderr = process.communicate(input=b"\n", timeout=40)
        empty = BoundedTextCapture(CaptureLimit()).finish()
        result = CommandResult(process.returncode, empty, empty, 1, False)
        if case in ("short-delay", "job-child"):
            assert task.finish(result).exit_code == 0, (stdout, stderr)
            assert marker.read_bytes() == b"entered"
            if case == "job-child":
                # Job accounting may still include the exited main process.
                active = [int(line.split(b"=", 1)[1]) for line in stderr.splitlines()
                          if line.startswith(b"reap_job_active_before=")]
                assert active and active[0] > 0
                assert b"reap_job_active=0" in stderr
        else:
            # This also exercises native Fault reconciliation after process-first
            # wakeup and the existing Python lifecycle-error boundary.
            assert process.returncode == 125, (stdout, stderr)
            if case == "fault-exit-race":
                assert b"terminate_error=5 wait=0 exit=3221225794" in stderr
            if case == "access-denied-live":
                assert b"terminate_error=5 wait=258 exit=259" in stderr
                assert b"reap_job_active=0" in stderr
            expected = "stage=5, error=5" if case == "access-denied-live" else "stage=6, error=1460"
            with pytest.raises(RuntimeError, match=expected):
                task.finish(result)
            assert not marker.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
        task.close()
