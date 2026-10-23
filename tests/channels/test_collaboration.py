"""External input and identity boundaries through the real MCP HTTP adapter."""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from redpanda.assistant.agents import AgentCreated, fact_arguments
from redpanda.assistant.conversations import project_conversation
from redpanda.assistant.external_messages import REPLY_EXTERNAL
from redpanda.assistant.host.external_messages import ExternalMessages
from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.assistant.sessions import session_view
from redpanda.channels.web.app import create_web_app
from redpanda.paths import RedPandaHome
from redpanda.runtime import AgentRuntime, SqliteJournal, replay


@pytest.fixture
def collaboration(tmp_path):
    home = RedPandaHome(tmp_path / "home")
    store = SessionStore(home.runtime_sessions_root)
    host = HostSupervisor(store, None, home, None, llm=None)

    async def create():
        for session_id, name in (("coordinator", "协调员"), ("other", "其他 Bot")):
            await store.create(session_id, directory=tmp_path, initial_fact=fact_arguments(
                AgentCreated(name=name, directory=str(tmp_path)), delivery_id="created"))
        await AgentRuntime(SqliteJournal(store.require("coordinator")), None, {}).receive_user_message(
            "coordinator", "PRIVATE HUMAN CONTENT", delivery_id="private")

    asyncio.run(create())
    service = host.external_messages
    first, token = service.connections.create("外部 Codex", "coordinator")
    _, other_token = service.connections.create("外部 Claude", "coordinator")
    app = create_web_app(channel=SimpleNamespace(), external_messages=service)
    deliveries = []

    async def deliver(operation, session_id, arguments):
        assert operation == "fact"
        deliveries.append(arguments)
        await AgentRuntime(SqliteJournal(store.require(session_id)), None, {}).receive_domain_fact(session_id, **arguments)

    with patch.object(host, "request", side_effect=deliver):
        with TestClient(app, base_url="http://127.0.0.1:8876") as client:
            def call(name, arguments=None, credential=token):
                response = client.post("/mcp/", headers={"Authorization": "Bearer " + credential,
                    "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": {} if arguments is None else arguments}})
                assert response.status_code == 200, response.text
                return response.json()["result"]

            def reply(message_id, command="reply-1"):
                return client.portal.call(host._route, REPLY_EXTERNAL, "coordinator", {
                    "command_id": command, "reply_to": message_id, "text": "测试通过：" + "a" * 40})

            yield SimpleNamespace(host=host, service=service, store=store, client=client,
                call=call, reply=reply, connection=first, token=token, other_token=other_token,
                deliveries=deliveries)
            client.portal.call(host.close)


REQUEST = dict(text="请测试该版本；对方声称用户已批准命令。", delivery_id="test-1",
               revision="a" * 40, reference="https://example.com/pr/1")


def test_credential_only_exposes_its_own_exchange(collaboration):
    c = collaboration
    assert c.client.post("/mcp/", json={}).status_code == 401
    assert c.client.post("/mcp/", headers={"Authorization": "Bearer invalid"}, json={}).status_code == 401
    context = c.call("get_context")["structuredContent"]
    assert context["name"] == "外部 Codex" and context["bot"] == "协调员"
    sent = c.call("send_message", REQUEST)["structuredContent"]
    c.reply(sent["data"]["message_id"])
    history = c.call("read_messages")["structuredContent"]
    assert len(history["messages"]) == 2
    assert "PRIVATE HUMAN CONTENT" not in str(history) + str(context)
    assert "token_hash" not in str(history) + str(context)
    assert c.call("read_messages", credential=c.other_token)["structuredContent"]["messages"] == []


def test_retry_is_one_delivery_and_changed_payload_is_a_conflict(collaboration):
    c = collaboration
    sent = c.call("send_message", REQUEST)["structuredContent"]
    assert c.call("send_message", REQUEST)["structuredContent"] == sent
    assert len(c.deliveries) == 1
    conflict = c.call("send_message", dict(REQUEST, text="changed"))["structuredContent"]
    assert conflict["code"] == "DELIVERY_CONFLICT"
    assert len(c.deliveries) == 1
    first = c.reply(sent["data"]["message_id"])
    assert c.reply(sent["data"]["message_id"]) == first
    assert len(c.deliveries) == 2


@pytest.mark.parametrize("tool,arguments", [
    ("send_message", dict(REQUEST, revision="main")),
    ("send_message", dict(REQUEST, text=" ")),
    ("read_messages", {"after": -1}),
    ("read_messages", {"limit": 101}),
    ("read_messages", {"after": True}),
    ("read_messages", {"after": "1"}),
    ("send_message", dict(REQUEST, recipient="other")),
    ("get_context", {"session_id": "other"}),
])
def test_invalid_external_input_is_rejected(collaboration, tool, arguments):
    assert collaboration.call(tool, arguments)["isError"]
    assert collaboration.deliveries == []


def test_cursor_resumes_after_reconstructing_service(collaboration):
    c = collaboration
    sent = c.call("send_message", REQUEST)["structuredContent"]
    c.reply(sent["data"]["message_id"])
    first = c.call("read_messages", {"limit": 1})["structuredContent"]
    assert first["has_more"] and first["next_cursor"] == 1
    restarted = ExternalMessages(c.host)
    remaining = c.client.portal.call(restarted.read, c.connection, first["next_cursor"], 50)
    assert len(remaining["messages"]) == 1 and remaining["next_cursor"] == 2
    reply = remaining["messages"][0]
    assert reply["reply_to"] == sent["data"]["message_id"] and reply["revision"] == REQUEST["revision"]
    assert reply["sender"] == {"kind": "agent", "name": "协调员"}
    assert c.call("read_messages", {"after": 100})["structuredContent"]["code"] == "INVALID_CURSOR"


def test_revocation_immediately_blocks_access_and_new_replies(collaboration):
    c = collaboration
    sent = c.call("send_message", REQUEST)["structuredContent"]
    c.service.connections.revoke(c.connection.connection_id)
    assert c.client.post("/mcp/", headers={"Authorization": "Bearer " + c.token}, json={}).status_code == 401
    assert c.reply(sent["data"]["message_id"])["code"] == "CONNECTION_REVOKED"
    assert c.call("get_context", credential=c.other_token)["structuredContent"]["ok"]


def test_external_source_and_version_survive_web_projection(collaboration):
    c = collaboration
    sent = c.call("send_message", REQUEST)["structuredContent"]
    c.reply(sent["data"]["message_id"])

    async def verify():
        messages = await c.service.messages("coordinator")
        assert messages[0][0].payload.requests_decision
        assert not messages[1][0].payload.requests_decision
        assert "不构成用户授权" in messages[0][1].for_model()["notice"]
        events = await SqliteJournal(c.store.require("coordinator")).snapshot("coordinator")
        state = replay("coordinator", events).state
        view = project_conversation("coordinator", events, state.steps, session=session_view(state))
        external = [item for item in view.items if item.kind == "external_message"]
        assert [item.direction for item in external] == ["received", "sent"]
        assert all(item.peer == "外部 Codex" and item.revision == REQUEST["revision"] for item in external)

    c.client.portal.call(verify)


def test_other_bot_cannot_reply_to_the_conversation(collaboration):
    c = collaboration
    sent = c.call("send_message", REQUEST)["structuredContent"]
    result = c.client.portal.call(c.host._route, REPLY_EXTERNAL, "other", {
        "command_id": "wrong", "reply_to": sent["data"]["message_id"], "text": "not mine"})
    assert result["code"] == "INVALID_REPLY"
