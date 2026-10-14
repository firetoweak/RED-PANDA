import json
import threading
import unittest
from unittest.mock import Mock, patch

from redpanda.filesystem.file_view.client import Client


class FileViewClientTimingTest(unittest.TestCase):
    def test_internal_request_waits_for_reply_without_execution_deadline(self):
        client = object.__new__(Client)
        client.lock = threading.Lock()
        client.process = Mock()
        client.replies = Mock()
        client.replies.get.return_value = json.dumps({"status": "sealed"})
        with (
            patch("redpanda.filesystem.file_view.client.perf_counter", side_effect=(5, 65)),
            self.assertLogs("redpanda.filesystem.file_view.client", level="INFO") as captured,
        ):
            self.assertEqual(client.request("finish", command_id="private-command"), {"status": "sealed"})
        client.replies.get.assert_called_once_with()
        self.assertEqual(captured.records[0].elapsed_ms, 60000)
        self.assertEqual(captured.records[0].stage, "system")
        self.assertEqual(captured.records[0].request_operation, "finish")
        self.assertNotIn("private-command", str(captured.records[0].__dict__))
