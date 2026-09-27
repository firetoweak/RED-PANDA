from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from helperme.channels.web.directory_picker import (
    DirectoryPickerUnavailable,
    select_directory,
    select_file,
)


class DirectoryPickerTest(unittest.IsolatedAsyncioTestCase):
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
                with patch(
                    "helperme.channels.web.directory_picker.subprocess.Popen",
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
        with patch(
            "helperme.channels.web.directory_picker.subprocess.Popen",
            return_value=process,
        ):
            task = asyncio.create_task(select_directory())
            await asyncio.to_thread(started.wait)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        process.terminate.assert_called_once()
        process.wait.assert_called_once()

    async def test_select_file_asks_the_dialog_process_for_a_file(self):
        selected = Path(__file__).resolve()
        process = SimpleNamespace(
            returncode=0,
            communicate=lambda: (json.dumps(str(selected)).encode(), b""),
        )
        with patch(
            "helperme.channels.web.directory_picker.subprocess.Popen",
            return_value=process,
        ) as popen:
            self.assertEqual(await select_file(), selected)
        self.assertEqual(popen.call_args.args[0][-1], "--file")
