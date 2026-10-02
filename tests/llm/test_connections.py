import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from helperme.config import write_json
from helperme.llm.config import initial_connections
from helperme.llm.connections import ModelConnections
from helperme.llm.api import LLMAuthenticationError


class ConnectionsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "connections.json"
        self.settings = initial_connections()
        self.settings["deepseek"]["api_key"] = "old"
        write_json(self.path, self.settings)
        self.clients = []

        class Client:
            def __init__(client, endpoint):
                client.endpoint = endpoint
                client.closed = False
                client.started = asyncio.Event()
                client.release = asyncio.Event()
                self.clients.append(client)

            async def chat(client, messages, model, **kwargs):
                client.started.set()
                if messages == ["wait"]:
                    await client.release.wait()
                return (client.endpoint.api_key, model)

            async def __aexit__(client, *_args):
                client.closed = True

        self.connections = ModelConnections(self.path, client_factory=Client)

    async def asyncTearDown(self):
        await self.connections.__aexit__(None, None, None)
        self.directory.cleanup()

    async def test_hot_update_keeps_inflight_client_until_its_request_finishes(self):
        task = asyncio.create_task(self.connections.chat(["wait"], "deepseek/pro"))
        while not self.clients:
            await asyncio.sleep(0)
        old = self.clients[0]
        await old.started.wait()
        self.settings["deepseek"]["api_key"] = "new"
        write_json(self.path, self.settings)

        result = await self.connections.chat([], "deepseek/flash")
        self.assertEqual(result, ("new", "deepseek/flash"))
        self.assertFalse(old.closed)
        old.release.set()
        self.assertEqual(await task, ("old", "deepseek/pro"))
        self.assertTrue(old.closed)
        self.assertFalse(self.clients[1].closed)

    async def test_models_share_provider_connection_and_providers_are_independent(self):
        await self.connections.chat([], "deepseek/pro")
        await self.connections.chat([], "deepseek/flash")
        await self.connections.chat([], "ollama/local")
        self.assertEqual([client.endpoint.provider for client in self.clients], ["deepseek", "ollama"])

    async def test_local_address_hot_update_closes_idle_client(self):
        await self.connections.chat([], "ollama/local")
        old = self.clients[0]
        self.settings["ollama"]["base_url"] = "http://127.0.0.1:11435/v1"
        write_json(self.path, self.settings)
        await self.connections.chat([], "ollama/local")
        self.assertTrue(old.closed)
        self.assertEqual(self.clients[1].endpoint.base_url, "http://127.0.0.1:11435/v1")

    async def test_invalid_persisted_connection_is_not_replaced_by_old_client(self):
        await self.connections.chat([], "deepseek/pro")
        self.path.write_text("{", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            await self.connections.chat([], "deepseek/pro")

    async def test_removing_required_key_fails_without_reusing_old_credentials(self):
        await self.connections.chat([], "deepseek/pro")
        self.settings["deepseek"]["api_key"] = ""
        write_json(self.path, self.settings)
        with self.assertRaises(LLMAuthenticationError):
            await self.connections.chat([], "deepseek/pro")
