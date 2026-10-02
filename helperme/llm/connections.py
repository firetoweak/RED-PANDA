"""Host 复用连接；每次调用读取新配置，在途请求持有自己的客户端。"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path

from thinllm import ChatCompletionsClient, Endpoint, resolve_endpoint, MissingProviderSetting
from helperme.llm.config import load_connections
from helperme.llm.api import LLMAuthenticationError


@dataclass
class _Connection:
    endpoint: Endpoint
    client: object
    users: int = 0
    retired: bool = False


class ModelConnections:
    def __init__(self, path: Path, *, client_factory=ChatCompletionsClient):
        self.path = path
        self._factory = client_factory
        self._current: dict[str, _Connection] = {}
        self._retired: list[_Connection] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        for connection in [*self._current.values(), *self._retired]:
            await connection.client.__aexit__(exc_type, exc, traceback)
        self._current.clear()
        self._retired.clear()

    async def chat(self, messages, model, tools=None, **kwargs):
        provider = model.partition("/")[0]
        settings = load_connections(self.path)
        try:
            endpoint = resolve_endpoint(model, settings[provider])
        except MissingProviderSetting as error:
            raise LLMAuthenticationError(f"请在 connections.json 中设置 {provider}.{error.setting}") from error
        connection = self._current.get(provider)
        idle_previous = None
        if connection is None or connection.endpoint != endpoint:
            previous = connection
            connection = _Connection(endpoint, self._factory(endpoint))
            self._current[provider] = connection
            if previous is not None:
                previous.retired = True
                if previous.users:
                    self._retired.append(previous)
                else:
                    idle_previous = previous
        connection.users += 1
        try:
            if idle_previous is not None:
                await idle_previous.client.__aexit__(None, None, None)
            return await connection.client.chat(messages, model, tools=tools, **kwargs)
        finally:
            connection.users -= 1
            if connection.retired and connection.users == 0:
                self._retired.remove(connection)
                await connection.client.__aexit__(None, None, None)
