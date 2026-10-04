from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from redpanda.assistant.host.worker import run_worker


class WorkerFailureTest(unittest.IsolatedAsyncioTestCase):
    async def test_return_persistence_failure_preserves_both_original_errors(self):
        original = RuntimeError("initialization failed")
        reporting = OSError("journal write failed")
        with TemporaryDirectory() as directory:
            with (
                patch(
                    "redpanda.assistant.host.worker._run_session",
                    AsyncMock(side_effect=original),
                ),
                patch(
                    "redpanda.assistant.host.worker.record_unexpected_return",
                    AsyncMock(side_effect=reporting),
                ),
                self.assertRaises(ExceptionGroup) as caught,
            ):
                await run_worker(
                    None, "child", Path(directory) / "journal.sqlite", None, directory
                )
        self.assertEqual(caught.exception.exceptions, (original, reporting))
