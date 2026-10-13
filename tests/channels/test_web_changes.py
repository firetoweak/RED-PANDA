from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest

from redpanda.channels.web.app import create_web_app


def test_change_endpoint_rejects_paths_outside_workspace_and_stale_revision():
    query = AsyncMock(return_value={"revision": "a" * 64, "lines": [], "next_offset": None})
    with TestClient(create_web_app(channel=SimpleNamespace(file_changes=query))) as client:
        for path in ("../secret", "/secret", "C:/secret", "a\\b", "a/../b", ".", "a//b"):
            assert client.get("/api/sessions/s/changes", params={"path": path}).status_code == 422
        query.assert_not_awaited()
        assert client.get("/api/sessions/s/changes", params={"path": "src/app.py", "revision": "b" * 64}).status_code == 409
        assert client.get("/api/sessions/s/changes", params={"path": "src/app.py", "revision": "a" * 64}).status_code == 200
        query.return_value = None
        assert client.get("/api/sessions/s/changes", params={"path": "src/app.py"}).status_code == 404
        query.side_effect = ValueError("corrupt evidence")
        with pytest.raises(ValueError, match="corrupt evidence"):
            client.get("/api/sessions/s/changes")
