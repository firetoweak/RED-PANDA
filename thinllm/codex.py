"""Codex authentication for the shared Responses protocol."""
from thinllm.auth import CodexAuth
from thinllm.responses import ResponsesClient


class CodexResponsesClient(ResponsesClient):
    def __init__(self, endpoint, **kwargs):
        super().__init__(endpoint, auth=CodexAuth(endpoint.auth_file) if endpoint.auth_file else None, **kwargs)
