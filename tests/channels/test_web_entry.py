from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import web_chat


class WebEntryTests(unittest.TestCase):
    @patch("web_chat.host_process_environment", return_value={"PATH": "/application/tools"})
    @patch("web_chat.uvicorn.run")
    @patch("web_chat.subprocess.Popen")
    def test_dev_starts_and_stops_vite(self, popen, run, product_environment):
        frontend = Mock()
        frontend.poll.return_value = None
        popen.return_value = frontend

        web_chat.main(["--dev"])

        web = Path(web_chat.__file__).parent / "web"
        popen.assert_called_once_with(
            ["node", web / "node_modules" / "vite" / "bin" / "vite.js"],
            cwd=web,
            env={"PATH": "/application/tools"},
            creationflags=(web_chat.subprocess.CREATE_NEW_PROCESS_GROUP
                           if web_chat.os.name == "nt" else 0),
        )
        run.assert_called_once_with(
            "web_chat:app",
            host="127.0.0.1",
            port=8765,
            reload=True,
            reload_dirs=[str(Path(web_chat.__file__).parent)],
            timeout_graceful_shutdown=1,
        )
        frontend.terminate.assert_called_once_with()
        frontend.wait.assert_called_once_with()

