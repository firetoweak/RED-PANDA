from __future__ import annotations

import asyncio
import signal
import socket
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
import uvicorn

import web_chat
from konden.channels.web.app import create_web_app
from konden.channels.web.channel import WebChannel


@pytest.mark.process
def test_sigterm_shutdown_of_an_open_sse_has_no_cancelled_error_traceback(caplog):
    async def scenario():
        channel = WebChannel(SimpleNamespace(
            release=AsyncMock(), note_web_connected=Mock(), note_web_disconnected=Mock(),
        ), None)
        app = create_web_app(channel=channel)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, timeout_graceful_shutdown=0.05, log_config=None))
        # Exercise Uvicorn's SIGTERM handler without replacing pytest's process signal handlers.
        with patch.object(server, "capture_signals", return_value=nullcontext()):
            serving = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                # Loopback HTTP must not use machine proxies. Client setup loads
                # certificates synchronously, before the server's time budget.
                async with (httpx.AsyncClient(trust_env=False) as client,
                            asyncio.timeout(5)):
                    while not server.started:
                        await asyncio.sleep(0.01)
                    async with client.stream("GET", f"http://127.0.0.1:{port}/api/events") as response:
                        lines = response.aiter_lines()
                        first_line = await anext(lines)
                        assert first_line == "event: connected"
                        server.handle_exit(signal.SIGTERM, None)
                        await serving
            finally:
                server.should_exit = True
                if not serving.done():
                    await serving
                listener.close()
        assert not channel._connections
        assert channel._sessions.release.await_count == 1
    asyncio.run(scenario())
    assert "CancelledError" not in caplog.text
    assert "Exception in ASGI application" not in caplog.text


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
        run.assert_called_once()
        self.assertEqual(run.call_args.args, ("web_chat:app",))
        self.assertEqual(run.call_args.kwargs["host"], "127.0.0.1")
        self.assertEqual(run.call_args.kwargs["port"], 8765)
        self.assertTrue(run.call_args.kwargs["reload"])
        frontend.terminate.assert_called_once_with()
        frontend.wait.assert_called_once_with()
