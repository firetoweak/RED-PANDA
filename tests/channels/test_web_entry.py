from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import web_chat


class WebEntryTests(unittest.TestCase):
    @patch("web_chat.uvicorn.run")
    @patch("web_chat.subprocess.Popen")
    def test_dev_starts_and_stops_vite(self, popen, run):
        frontend = Mock()
        frontend.poll.return_value = None
        popen.return_value = frontend

        web_chat.main(["--dev"])

        web = Path(web_chat.__file__).parent / "web"
        popen.assert_called_once_with(
            ["node", web / "node_modules" / "vite" / "bin" / "vite.js"],
            cwd=web,
        )
        run.assert_called_once_with(
            "web_chat:app",
            host="0.0.0.0",
            port=8765,
            reload=True,
            reload_dirs=[str(Path(web_chat.__file__).parent)],
            timeout_graceful_shutdown=1,
        )
        frontend.terminate.assert_called_once_with()
        frontend.wait.assert_called_once_with()

    @patch("web_chat.uvicorn.run")
    @patch("web_chat.create_web_app")
    def test_port_option_overrides_default(self, create_web_app, run):
        web_chat.main(["--port", "8766"])

        self.assertEqual(run.call_args.kwargs["port"], 8766)
