"""A separate subreaper owns each Linux task, including setsid descendants.

The control pipe is never inherited by the task. Host exit closes it, allowing
the owner to reap its tree even when the Host itself was killed.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


class Running:
    def __init__(self, argv, cwd, environment, log_dir):
        self._owner = None
        self._closed = False
        self._diagnostics = (Path(log_dir) / "owner.log").open("wb")
        try:
            self._owner = subprocess.Popen(
                [sys.executable, "-I", "-u", str(Path(__file__).resolve())],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._diagnostics,
                text=True, encoding="utf-8", close_fds=True, start_new_session=True,
            )
            response = self._request({"argv": [str(arg) for arg in argv], "cwd": str(cwd),
                                      "environment": environment, "log_dir": str(log_dir)})
            if "start_error" in response:
                error = response["start_error"]
                raise OSError(error["errno"], error["message"])
            self.pid = response["pid"]
        except BaseException:
            self.close()
            raise

    def _request(self, request):
        self._owner.stdin.write(json.dumps(request) + "\n")
        self._owner.stdin.flush()
        response = self._owner.stdout.readline()
        if not response:
            raise RuntimeError("Linux process owner exited without a response; inspect owner.log")
        return json.loads(response)

    def poll(self):
        return self._request({"operation": "poll"})["exit_code"]

    def close(self):
        if self._closed:
            return
        try:
            if self._owner is not None:
                self._owner.stdin.close()
                code = self._owner.wait(timeout=15)
                if code != 0:
                    raise RuntimeError(f"Linux process owner failed during cleanup: exit_code={code}; inspect owner.log")
            self._closed = True
        finally:
            if self._owner is not None:
                self._owner.stdout.close()
            self._diagnostics.close()


def _serve():
    # -I ignores user PYTHONPATH/PYTHONHOME; only this implementation directory is added.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from process_posix import Running as Task

    def send(response):
        print(json.dumps(response), flush=True)

    request = json.loads(sys.stdin.readline())
    try:
        task = Task(request["argv"], request["cwd"], request["environment"], request["log_dir"])
    except OSError as exc:
        send({"start_error": {"errno": exc.errno, "message": str(exc)}})
        return
    try:
        send({"pid": task.pid})
        for line in sys.stdin:
            request = json.loads(line)
            if request["operation"] != "poll":
                raise ValueError("Unknown Linux process owner operation")
            send({"exit_code": task.poll()})
    finally:
        task.close()


if __name__ == "__main__":
    _serve()
