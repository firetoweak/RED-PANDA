from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import sys
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from konden.channels.web.directory_picker import (
    DirectoryPickerUnavailable,
    select_directory,
    select_file,
    _main,
)


class DirectoryPickerTest(unittest.IsolatedAsyncioTestCase):
    def test_dialog_creation_failure_is_known_unavailability_and_cleans_up_root(self):
        class TclError(Exception):
            pass

        root = Mock()
        tkinter = SimpleNamespace(TclError=TclError, Tk=Mock(return_value=root),
            filedialog=SimpleNamespace(askdirectory=Mock(side_effect=TclError("desktop unavailable"))))
        with (patch.dict(sys.modules, {"tkinter": tkinter}),
              patch("konden.channels.web.directory_picker.sys", SimpleNamespace(
                  platform="linux", argv=["picker"], stderr=sys.stderr)),
              patch.object(sys.stderr, "reconfigure")):
            with self.assertRaises(SystemExit) as raised:
                _main()
        self.assertEqual(raised.exception.code, 2)
        root.destroy.assert_called_once()

    async def test_macos_native_selection_preserves_path_cancel_and_failure(self):
        path = Path(__file__).resolve().parent / "资料\n文件.txt\n"
        for select, kind in ((select_directory, "folder"), (select_file, "file")):
            for selected in (str(path), ""):
                with self.subTest(kind=kind, selected=selected):
                    process = SimpleNamespace(returncode=0, communicate=lambda: (
                        (selected + "\n").encode("utf-8"), b"",
                    ))
                    with (patch("konden.channels.web.directory_picker.sys", SimpleNamespace(platform="darwin")),
                          patch("konden.channels.web.directory_picker.subprocess.Popen", return_value=process) as popen):
                        self.assertEqual(await select(), path if selected else None)
                    self.assertEqual(popen.call_args.args[0][0], "/usr/bin/osascript")
                    self.assertIn("choose " + kind, popen.call_args.args[0][-1])
        process = SimpleNamespace(returncode=2, communicate=lambda: (b"", b"original failure"))
        with (patch("konden.channels.web.directory_picker.sys", SimpleNamespace(platform="darwin")),
              patch("konden.channels.web.directory_picker.subprocess.Popen", return_value=process)):
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                await select_directory()
            self.assertEqual(raised.exception.stderr, b"original failure")

    async def test_process_protocol_preserves_selection_cancel_and_error_meanings(self):
        selected = Path(__file__).resolve().parent
        cases = [
            (0, json.dumps(str(selected)).encode(), b"", selected),
            (0, b"null", b"", None),
            (2, b"", "没有图形桌面".encode(), DirectoryPickerUnavailable),
            (1, b"", b"original traceback", subprocess.CalledProcessError),
            (0, b'"relative/path"', b"", ValueError),
        ]
        for returncode, stdout, stderr, expected in cases:
            with self.subTest(returncode=returncode, stdout=stdout):
                process = SimpleNamespace(
                    returncode=returncode,
                    communicate=lambda: (stdout, stderr),
                )
                with patch("konden.channels.web.directory_picker.sys", SimpleNamespace(
                    platform="linux", executable=sys.executable,
                )), patch(
                    "konden.channels.web.directory_picker.subprocess.Popen",
                    return_value=process,
                ):
                    if isinstance(expected, type):
                        with self.assertRaises(expected) as raised:
                            await select_directory()
                        if expected is subprocess.CalledProcessError:
                            self.assertEqual(raised.exception.stderr, stderr)
                        elif expected is DirectoryPickerUnavailable:
                            self.assertEqual(str(raised.exception), stderr.decode())
                    else:
                        self.assertEqual(await select_directory(), expected)

    async def test_cancelled_request_terminates_and_reaps_dialog_process(self):
        started, finished = Event(), Event()

        def communicate():
            started.set()
            finished.wait()
            return b"null", b""

        process = SimpleNamespace(
            communicate=communicate,
            poll=lambda: None,
            terminate=Mock(side_effect=finished.set),
            wait=Mock(return_value=-1),
        )
        with patch("konden.channels.web.directory_picker.sys", SimpleNamespace(
            platform="darwin", executable=sys.executable,
        )), patch(
            "konden.channels.web.directory_picker.subprocess.Popen",
            return_value=process,
        ) as popen:
            task = asyncio.create_task(select_directory())
            await asyncio.to_thread(started.wait)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        process.terminate.assert_called_once()
        process.wait.assert_called_once()
        self.assertEqual(popen.call_args.args[0][0], "/usr/bin/osascript")

    async def test_select_file_asks_the_dialog_process_for_a_file(self):
        selected = Path(__file__).resolve()
        process = SimpleNamespace(
            returncode=0,
            communicate=lambda: (json.dumps(str(selected)).encode(), b""),
        )
        with patch("konden.channels.web.directory_picker.sys", SimpleNamespace(
            platform="linux", executable=sys.executable,
        )), patch(
            "konden.channels.web.directory_picker.subprocess.Popen",
            return_value=process,
        ) as popen:
            self.assertEqual(await select_file(), selected)
        self.assertEqual(popen.call_args.args[0][-1], "--file")
