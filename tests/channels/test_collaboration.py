"""External conversation behavior at the MCP and human Web boundaries."""
import asyncio
from datetime import datetime, timezone
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from konden.assistant.agents import AgentCreated, fact_arguments
from konden.assistant.conversations import project_conversation
from konden.assistant.external_messages import LIST_EXTERNAL, SEND_EXTERNAL, SendToExternal
from konden.assistant.external_messages import parse_external
from konden.assistant.host.external_messages import ExternalMessages
from konden.assistant.host.codex_continuation import QueueAttempt
from konden.assistant.host.session_store import SessionStore
from konden.assistant.host.supervisor import HostSupervisor
from konden.assistant.sessions import session_view
from konden.channels.web.app import create_web_app
from konden.channels.web.channel import WebChannel
from konden.channels.web.hub import WebEventHub
from konden.paths import KondenHome
from konden.runtime import AgentRuntime, SqliteJournal, replay


@pytest.fixture
def collaboration(tmp_path):
    home = KondenHome(tmp_path / "home")
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
    first, token = service.connections.create("test")
    _, other_token = service.connections.create("test")
    web = WebChannel(host, None)
    hub = WebEventHub()
    app = create_web_app(channel=web, hub=hub, external_messages=service)
    deliveries = []

    async def deliver(operation, session_id, arguments):
        assert operation == "fact"
        deliveries.append(arguments)
        await AgentRuntime(SqliteJournal(store.require(session_id)), None, {}).receive_domain_fact(session_id, **arguments)

    with patch("konden.assistant.host.external_messages.read_codex_titles", new=AsyncMock(return_value={})) as titles, \
            patch.object(host, "request", side_effect=deliver):
        with TestClient(app, base_url="http://127.0.0.1:8876") as client:
            human = client.portal.call(web.connect)

            native_identity = "0123456789ab7def8123456789abcdef"

            def call(name, arguments=None, credential=token, *, chat=native_identity, meta="native"):
                metadata = {"threadId": str(UUID(chat))} if meta == "native" else meta
                response = client.post("/mcp/", headers={"Authorization": "Bearer " + credential,
                    "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": {} if arguments is None else arguments,
                                     "_meta": metadata}})
                assert response.status_code == 200, response.text
                return response.json()["result"]

            context = call("get_context")["structuredContent"]
            identity = context["identity"]

            def send(**changes):
                chat = changes.pop("identity", identity)
                arguments = dict(recipient="协调员", text="你好，来讨论一下需求。", delivery_id="hello")
                arguments.update(changes)
                return call("send_message", arguments, chat=chat)["structuredContent"]

            def reply(command="reply-1", session_id="coordinator", **changes):
                arguments = dict(identity=identity, text="收到，我们可以继续讨论。", reply_to=None)
                arguments.update(changes)
                parsed = SendToExternal(**arguments).model_dump()
                return client.portal.call(host._route, SEND_EXTERNAL, session_id, {"command_id": command, **parsed})

            def read(chat_identity=identity, credential=token, **options):
                arguments = dict(wait_seconds=0)
                arguments.update(options)
                return call("read_messages", arguments, credential, chat=chat_identity)["structuredContent"]

            yield SimpleNamespace(host=host, service=service, store=store, client=client, hub=hub,
                call=call, send=send, read=read, reply=reply, identity=identity, context=context,
                connection=first, token=token, other_token=other_token, deliveries=deliveries,
                human_connection=human.connection_id, titles=titles)
            client.portal.call(web.disconnect, human)
            client.portal.call(host.close)


def test_web_manages_named_access(collaboration):
    c = collaboration
    path = "/api/external-connections"
    assert c.client.post(path, json={"connection_id": "inactive", "name": "test"}).status_code == 409
    before = c.client.get(path).json()
    for fields in ({}, {"name": ""}, {"name": "   "}):
        assert c.client.post(path, json={"connection_id": c.human_connection, **fields}).status_code == 422
    assert c.client.get(path).json() == before
    created = c.client.post(path, json={"connection_id": c.human_connection, "name": "笔记本 Codex"})
    assert created.status_code == 201 and created.headers["cache-control"] == "no-store"
    credential = created.json()
    assert credential["url"] == "http://127.0.0.1:8876/mcp/"
    entries = c.client.get(path).json()
    assert credential["connection"] in entries and all(set(item) == {"connection_id", "name"} for item in entries)
    assert credential["token"] not in str(entries)
    context = c.call("get_context", credential=credential["token"], chat="3" * 32)["structuredContent"]
    assert {item["name"] for item in context["colleagues"]} == {"协调员", "其他 Bot"}
    external_id = credential["connection"]["connection_id"]
    config_path = f"{path}/{external_id}/configuration"
    assert c.client.post(config_path, json={"connection_id": "inactive"}).status_code == 409
    config = c.client.post(config_path, json={"connection_id": c.human_connection})
    assert config.headers["cache-control"] == "no-store"
    assert config.json() == credential
    restarted = ExternalMessages(c.host)
    assert restarted.connection_configuration(external_id)["token"] == credential["token"]
    rename_path = f"{path}/{external_id}/name"
    assert c.client.put(rename_path, json={"connection_id": "inactive", "name": "x"}).status_code == 409
    assert c.client.put(rename_path, json={"connection_id": c.human_connection, "name": "x" * 81}).status_code == 422
    renamed = c.client.put(rename_path, json={"connection_id": c.human_connection, "name": " 台式机 Codex "})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "台式机 Codex"
    c.call("read_messages", {"wait_seconds": 0}, credential["token"], chat=context["identity"])
    restored = ExternalMessages(c.host).list_connections()
    item = next(item for item in restored if item["connection_id"] == external_id)
    assert item["name"] == "台式机 Codex"
    assert credential["token"] not in str(restored) and "token_hash" not in str(restored)
    assert c.client.request("DELETE", f"{path}/{external_id}", json={"connection_id": c.human_connection}).status_code == 204
    assert c.client.put(rename_path, json={"connection_id": c.human_connection, "name": "x"}).status_code == 404
    assert c.client.post(config_path, json={"connection_id": c.human_connection}).status_code == 404
    assert c.client.request("DELETE", f"{path}/{external_id}", json={"connection_id": c.human_connection}).status_code == 404
    assert c.client.post("/mcp/", headers={"Authorization": "Bearer " + credential["token"]}, json={}).status_code == 401


def test_registration_is_visible_before_messages_and_reconnection_preserves_identity(collaboration):
    c = collaboration
    queue = c.client.portal.call(c.hub.subscribe)
    fresh = c.call("get_context", {"name": "文档协作"}, chat="2" * 32)["structuredContent"]
    assert queue.get_nowait().name == "external_sessions_changed"
    assert fresh["identity"] != c.identity
    assert fresh["name"] == "文档协作"
    entries = c.client.get("/api/external-sessions").json()
    assert {item["identity"] for item in entries} == {c.identity, fresh["identity"]}
    assert all(item["peers"] == [] and item["active"] for item in entries)
    view = c.client.get("/api/external-sessions/" + fresh["identity"]).json()
    assert view["messages"] == [] and view["session"]["name"] == fresh["name"]
    assert c.call("get_context", chat=fresh["identity"])["structuredContent"] == fresh
    assert len(c.client.get("/api/external-sessions").json()) == 2
    restarted = ExternalMessages(c.host)
    restored = c.client.portal.call(restarted.context, c.connection, fresh["identity"])
    assert restored == fresh
    peers = c.client.portal.call(c.host._route, LIST_EXTERNAL, "coordinator", {})["data"]["sessions"]
    assert {item["identity"] for item in peers} == {c.identity, fresh["identity"]}
    assert all(set(item) == {"identity", "name", "active", "last_activity_at", "codex_status"} for item in peers)
    assert len(c.client.portal.call(c.host.agents.list)) == 2
    assert c.client.get("/api/external-sessions/unknown").status_code == 404


def test_codex_titles_follow_renames_without_changing_identity_history_or_explicit_alias(collaboration):
    c = collaboration
    c.titles.return_value = {c.identity: "了解 Tabbit CLI 与 Playwright 的关系"}
    context = c.call("get_context")["structuredContent"]
    assert context["identity"] == c.identity and context["name"] == c.titles.return_value[c.identity]
    original = c.send()["message_id"]
    c.reply(reply_to=original)
    history = c.read()
    before = c.service.sessions.read()

    c.titles.return_value = {c.identity: "浏览器工具选型"}
    entries = c.client.get("/api/external-sessions").json()
    assert len(entries) == 1 and entries[0]["name"] == "浏览器工具选型"
    view = c.client.get("/api/external-sessions/" + c.identity).json()
    assert view["session"]["name"] == "浏览器工具选型"
    peers = c.client.portal.call(c.host._route, LIST_EXTERNAL, "coordinator", {})["data"]["sessions"]
    assert peers[0]["name"] == "浏览器工具选型"
    assert c.read()["messages"] == history["messages"]
    assert c.service.sessions.read() == before
    messages = c.client.portal.call(c.service.messages, "coordinator")
    assert messages[0][1].name == context["name"]

    c.titles.return_value = {}
    c.call("get_context", {"name": "我的别名"})
    c.titles.reset_mock()
    assert c.client.get("/api/external-sessions").json()[0]["name"] == "我的别名"
    c.titles.assert_awaited_once_with([])


def test_access_and_identity_scope_messages_without_exposing_private_timelines(collaboration):
    c = collaboration
    assert c.client.post("/mcp/", json={}).status_code == 401
    assert c.client.post("/mcp/", headers={"Authorization": "Bearer invalid"}, json={}).status_code == 401
    c.send()
    c.reply()
    history = c.read()
    assert len(history["messages"]) == 1
    assert "PRIVATE HUMAN CONTENT" not in str(history) + str(c.context)
    assert "token_hash" not in str(history) + str(c.context)
    assert c.read(credential=c.other_token)["code"] == "SESSION_NOT_FOUND"
    assert c.read(chat_identity="2" * 32)["code"] == "SESSION_NOT_FOUND"
    assert c.call("get_context", credential=c.other_token)["structuredContent"]["code"] == "CONNECTION_CONFLICT"
    assert c.send(identity="2" * 32)["code"] == "SESSION_NOT_FOUND"


def test_native_chat_is_stable_and_descendant_tools_address_the_original_chat(collaboration):
    c = collaboration
    assert c.call("get_context")["structuredContent"]["identity"] == c.identity
    renamed = c.call("get_context", {"name": " 原生通信 "}, meta={
        "threadId": str(UUID("3" * 32)), "sessionId": str(UUID(c.identity)), "windowId": "window:2",
    })["structuredContent"]
    assert renamed["identity"] == c.identity and renamed["name"] == "原生通信"
    assert len(c.client.get("/api/external-sessions").json()) == 1
    restored = c.client.portal.call(ExternalMessages(c.host).context, c.connection, c.identity)
    assert restored == renamed
    c.reply(text="只给原来的聊天")
    result = c.call("read_messages", {"wait_seconds": 0}, meta={
        "threadId": str(UUID("3" * 32)), "sessionId": str(UUID(c.identity)),
    })["structuredContent"]
    assert result["messages"][0]["text"] == "只给原来的聊天"


@pytest.mark.parametrize("meta", [None, {}, {"threadId": "wrong"}, {"threadId": 5},
    {"threadId": "01234567-89ab-7def-8123-456789abcdef", "sessionId": "wrong"}])
def test_missing_or_invalid_native_metadata_cannot_create_or_address_a_chat(collaboration, meta):
    c = collaboration
    for tool, arguments in [("get_context", {}), ("read_messages", {"wait_seconds": 0}),
                             ("send_message", {"recipient": "协调员", "text": "hello", "delivery_id": "invalid"})]:
        result = c.call(tool, arguments, meta=meta)
        assert result["isError"] and "原生聊天标识" in result["content"][0]["text"]
    assert len(c.client.get("/api/external-sessions").json()) == 1 and c.deliveries == []


def test_new_chats_share_access_but_keep_their_messages_separate(collaboration):
    c = collaboration
    second = c.call("get_context", chat="2" * 32)["structuredContent"]["identity"]
    one = c.send()
    two = c.send(identity=second)
    assert one["message_id"] != two["message_id"]
    c.reply(identity=second, text="第二个聊天", command="second")
    c.reply(text="第一个聊天", command="first")
    assert [message["text"] for message in c.read()["messages"]] == ["第一个聊天"]
    assert [message["text"] for message in c.read(chat_identity=second)["messages"]] == ["第二个聊天"]


def test_revoked_configuration_can_be_replaced_without_losing_the_native_chat_history(collaboration):
    c = collaboration
    sent = c.send()
    c.reply(reply_to=sent["message_id"])
    first = c.read()
    c.client.portal.call(c.service.revoke_connection, c.connection.connection_id)
    restored = c.call("get_context", credential=c.other_token)["structuredContent"]
    assert restored["identity"] == c.identity
    assert len(c.client.get("/api/external-sessions").json()) == 1
    assert c.read(credential=c.other_token)["messages"] == first["messages"]
    assert c.read(credential=c.other_token, after=first["next_cursor"])["messages"] == []
    c.reply(command="after-rotation", text="换配置后的后续消息")
    result = c.read(credential=c.other_token, after=first["next_cursor"])
    assert result["messages"][0]["text"] == "换配置后的后续消息"
    assert result["next_cursor"] > first["next_cursor"]
    restarted = c.client.portal.call(ExternalMessages(c.host).view, c.identity)
    assert len(restarted["messages"]) == 3 and restarted["session"]["active"]
    assert restarted["codex"]["status"] == "disabled"


def test_messages_are_idempotent_and_replies_can_continue_the_same_conversation(collaboration):
    c = collaboration
    sent = c.send()
    assert sent["code"] == "DELIVERED"
    assert c.send() == sent and len(c.deliveries) == 1
    assert c.send(text="改了正文")["code"] == "DELIVERY_CONFLICT"
    assert c.send(recipient="其他 Bot")["code"] == "DELIVERY_CONFLICT"
    response = c.reply(reply_to=sent["message_id"])
    assert c.reply(reply_to=sent["message_id"]) == response
    c.send(delivery_id="continue", text="继续讨论", reply_to=response["message_id"])
    c.reply(command="follow-up", text="我又有了新发现。")
    assert [item["text"] for item in c.read()["messages"]] == ["收到，我们可以继续讨论。", "我又有了新发现。"]
    assert len(c.client.get("/api/external-sessions/" + c.identity).json()["messages"]) == 4


@pytest.mark.parametrize("tool,arguments", [
    ("send_message", {"recipient": "协调员", "text": " "}),
    ("send_message", {"text": "你好"}),
    ("send_message", {"recipient": "协调员", "text": "你好", "revision": "a" * 40}),
    ("send_message", {"recipient": "协调员", "text": "你好", "sender": "user"}),
    ("read_messages", {"after": -1}),
    ("read_messages", {"wait_seconds": 51}),
    ("read_messages", {"after": True}),
    ("get_context", {"identity": " "}),
    ("get_context", {"name": " "}),
    ("get_context", {"name": "x" * 81}),
    ("read_messages", {"identity": "0" * 32}),
    ("request_verification", {}),
    ("read_feedback", {}),
])
def test_invalid_inputs_are_rejected_before_delivery(collaboration, tool, arguments):
    c = collaboration
    fields = {}
    if tool == "send_message":
        fields["delivery_id"] = "invalid"
    assert c.call(tool, {**fields, **arguments})["isError"]
    assert c.deliveries == []


def test_bot_can_initiate_and_multiple_bots_share_a_stable_inbox_cursor(collaboration):
    c = collaboration
    assert c.read()["messages"] == []
    c.reply(text="我是主动来联系你的。")
    c.reply(command="other-1", session_id="other", text="我也来补充。")
    c.reply(command="more", text="第三条消息。")
    first = c.read(limit=2)
    assert [item["sender"]["name"] for item in first["messages"]] == ["协调员", "其他 Bot"]
    assert first["has_more"]
    restarted = ExternalMessages(c.host)
    remaining = c.client.portal.call(restarted.read, c.connection, c.identity, first["next_cursor"], 50, 0)
    assert [item["text"] for item in remaining["messages"]] == ["第三条消息。"]
    assert not remaining["has_more"] and remaining["next_cursor"] > first["next_cursor"]
    assert c.read(after=remaining["next_cursor"])["messages"] == []
    assert c.read(after=99)["code"] == "INVALID_CURSOR"


def test_empty_external_registry_does_not_read_bot_histories(tmp_path):
    home = KondenHome(tmp_path / "home")
    service = ExternalMessages(SimpleNamespace(home=home, agents=SimpleNamespace(
        list=AsyncMock(side_effect=AssertionError("empty external list must not read Bots")))))
    assert asyncio.run(service.list_sessions()) == []


def test_external_index_reads_only_new_facts_and_retries_a_durable_reply_after_lost_ack(collaboration):
    c = collaboration
    assert c.read()["messages"] == []
    deliver = c.host.request.side_effect

    async def lost_ack(operation, session_id, arguments):
        await deliver(operation, session_id, arguments)
        raise RuntimeError("transport acknowledgement lost")

    with patch.object(c.host, "request", side_effect=lost_ack):
        with pytest.raises(RuntimeError, match="acknowledgement lost"):
            c.reply()
    with patch("konden.assistant.host.external_messages.parse_external", wraps=parse_external) as parse:
        retried = c.reply()
        assert retried["code"] == "DELIVERED"
        first = c.read()
        assert len(first["messages"]) == 1 and first["next_cursor"] == 1
        assert parse.call_count == 1

        async def independent_write():
            await AgentRuntime(SqliteJournal(c.store.require("coordinator")), None, {}).receive_user_message(
                "coordinator", "unrelated Worker fact", delivery_id="unrelated")

        c.client.portal.call(independent_write)
        assert c.read(after=first["next_cursor"])["messages"] == []
        c.reply(command="next", session_id="other", text="第二条")
        second = c.read(after=first["next_cursor"])
        assert [message["text"] for message in second["messages"]] == ["第二条"]
        assert second["next_cursor"] == 2 and parse.call_count == 2
        assert len(c.deliveries) == 2


def test_waiting_reader_receives_messages_and_revocation_interrupts_wait(collaboration):
    c = collaboration
    waiting = c.client.portal.start_task_soon(c.service.read, c.connection, c.identity, 0, 50, 2)
    c.reply()
    result = waiting.result(timeout=3)
    assert result["messages"][0]["text"] == "收到，我们可以继续讨论。"
    waiting = c.client.portal.start_task_soon(c.service.read, c.connection, c.identity, result["next_cursor"], 50, 2)
    c.client.portal.call(c.service.revoke_connection, c.connection.connection_id)
    assert waiting.result(timeout=3)["code"] == "CONNECTION_REVOKED"
    assert c.reply(command="after-revoked")["code"] == "CONNECTION_REVOKED"
    view = c.client.get("/api/external-sessions/" + c.identity).json()
    assert not view["session"]["active"] and len(view["messages"]) == 1


def test_human_can_remove_one_chat_without_revoking_shared_access_or_erasing_bot_history(collaboration):
    c = collaboration
    original = c.send()["message_id"]
    c.reply(reply_to=original)
    history = c.read()
    before = c.client.portal.call(c.service.messages, "coordinator")
    other = c.call("get_context", chat="2" * 32)["structuredContent"]["identity"]
    path = f"/api/external-sessions/{c.identity}"
    assert c.client.put(path + "/codex", json={"connection_id": c.human_connection,
        "executable": sys.executable}).status_code == 200
    assert c.client.request("DELETE", path, json={"connection_id": "inactive"}).status_code == 409
    assert c.call("delete_external_session", {"identity": c.identity})["isError"]
    assert len(c.client.get("/api/external-sessions").json()) == 2

    changes = c.client.portal.call(c.hub.subscribe)
    waiting = c.client.portal.start_task_soon(c.service.read, c.connection, c.identity, history["next_cursor"], 50, 50)
    assert c.client.request("DELETE", path, json={"connection_id": c.human_connection}).status_code == 204
    assert changes.get_nowait().name == "external_sessions_changed"
    assert waiting.result(timeout=3)["code"] == "SESSION_NOT_FOUND"
    assert c.read()["code"] == c.send()["code"] == c.reply(command="removed")["code"] == "SESSION_NOT_FOUND"
    assert c.client.get(path).status_code == 404
    assert c.client.request("DELETE", path, json={"connection_id": c.human_connection}).status_code == 404
    assert c.identity not in c.service.codex.read().bindings
    assert [item["identity"] for item in c.client.get("/api/external-sessions").json()] == [other]
    restarted = ExternalMessages(c.host)
    assert c.client.portal.call(restarted.list_sessions)[0]["identity"] == other
    assert c.client.portal.call(c.service.messages, "coordinator") == before

    restored = c.call("get_context")["structuredContent"]
    assert restored["identity"] == c.identity
    assert c.read()["messages"] == history["messages"]
    assert c.client.get(path).json()["codex"]["status"] == "disabled"


def test_revoked_chat_can_be_removed_without_removing_other_chats(collaboration):
    c = collaboration
    c.client.portal.call(c.service.revoke_connection, c.connection.connection_id)
    path = f"/api/external-sessions/{c.identity}"
    assert not c.client.get(path).json()["session"]["active"]
    assert c.client.request("DELETE", path, json={"connection_id": c.human_connection}).status_code == 204
    assert c.client.get("/api/external-sessions").json() == []
    assert c.client.portal.call(ExternalMessages(c.host).list_sessions) == []


def test_codex_binding_is_human_owned_and_scoped_to_one_chat(collaboration):
    c = collaboration
    path = f"/api/external-sessions/{c.identity}/codex"
    target = {"connection_id": c.human_connection, "executable": sys.executable}
    assert c.client.put(path, json={**target, "connection_id": "inactive"}).status_code == 409
    assert c.client.put(path, json={**target, "thread_id": "22222222-2222-4222-8222-222222222222"}).status_code == 422
    assert c.client.put(path, json={**target, "executable": "nonexistent-konden-codex"}).status_code == 422
    assert c.call("configure_codex", target)["isError"]
    configured = c.client.put(path, json=target)
    assert configured.status_code == 200
    assert configured.json()["codex"]["status"] == "ready"
    second = c.call("get_context", chat="2" * 32)["structuredContent"]["identity"]

    async def queued(binding, message_id):
        assert binding.thread_id == str(UUID(c.identity))
        received = await c.service.read(c.connection, c.identity, 0, 50, 0)
        assert any(message["message_id"] == message_id for message in received["messages"])
        return QueueAttempt(message_id=message_id, attempted_at=datetime.now(timezone.utc),
                            status="queued", detail="已入队，尚未确认处理。")

    with patch("konden.assistant.host.codex_continuation.queue_codex", side_effect=queued) as queue:
        c.send()
        c.reply(identity=second, command="second-chat")
        assert queue.call_count == 0
        c.reply()
        c.reply()
        assert queue.call_count == 1
        view = c.client.get(f"/api/external-sessions/{c.identity}").json()
        assert view["codex"]["status"] == "queued" and view["codex"]["thread_id"] == str(UUID(c.identity))
        assert c.client.get(f"/api/external-sessions/{second}").json()["codex"]["status"] == "disabled"
        restarted = c.client.portal.call(ExternalMessages(c.host).view, c.identity)
        assert restarted["codex"] == view["codex"]
        assert c.client.request("DELETE", path, json={"connection_id": c.human_connection}).status_code == 200
        c.reply(command="disabled")
        assert queue.call_count == 1


def test_codex_failure_preserves_messages_and_can_be_retried(collaboration):
    c = collaboration
    path = f"/api/external-sessions/{c.identity}/codex"
    c.client.put(path, json={"connection_id": c.human_connection,
        "executable": sys.executable}).raise_for_status()

    async def attempt(binding, message_id):
        return QueueAttempt(message_id=message_id, attempted_at=datetime.now(timezone.utc),
                            status="failed", detail="Codex 没有运行")

    with patch("konden.assistant.host.codex_continuation.queue_codex", side_effect=attempt) as queue:
        sent = c.reply()
        assert sent["code"] == "DELIVERED" and len(c.read()["messages"]) == 1
        view = c.client.get(f"/api/external-sessions/{c.identity}").json()
        assert view["codex"]["status"] == "failed" and view["codex"]["detail"] == "Codex 没有运行"
        assert c.client.post(path + "/retry", json={"connection_id": "inactive"}).status_code == 409
        queue.side_effect = lambda binding, message_id: QueueAttempt(message_id=message_id,
            attempted_at=datetime.now(timezone.utc), status="queued", detail="已入队")
        retried = c.client.post(path + "/retry", json={"connection_id": c.human_connection})
        assert retried.status_code == 200 and retried.json()["codex"]["status"] == "queued"
        assert len(c.read()["messages"]) == 1
        c.client.portal.call(c.service.revoke_connection, c.connection.connection_id)
        assert c.client.post(path + "/retry", json={"connection_id": c.human_connection}).status_code == 422
        assert queue.call_count == 2


@pytest.mark.parametrize("delete_session", [False, True])
def test_slow_codex_does_not_block_reading_or_reenable_a_removed_binding(collaboration, delete_session):
    c = collaboration
    path = f"/api/external-sessions/{c.identity}/codex"
    c.client.put(path, json={"connection_id": c.human_connection,
        "executable": sys.executable}).raise_for_status()
    entered, release = asyncio.Event(), asyncio.Event()

    async def queue(binding, message_id):
        entered.set()
        await release.wait()
        return QueueAttempt(message_id=message_id, attempted_at=datetime.now(timezone.utc), status="queued", detail="已入队")

    with patch("konden.assistant.host.codex_continuation.queue_codex", side_effect=queue):
        sending = c.client.portal.start_task_soon(c.service.send_from_bot, "coordinator",
            {"command_id": "slow", "identity": c.identity, "text": "消息已到", "reply_to": None})
        c.client.portal.call(entered.wait)
        assert c.read()["messages"][0]["text"] == "消息已到"
        restarted = c.client.portal.call(ExternalMessages(c.host).view, c.identity)
        assert restarted["codex"]["status"] == "unknown"
        response = c.client.request("DELETE", path.removesuffix("/codex") if delete_session else path,
                                    json={"connection_id": c.human_connection})
        assert response.status_code == (204 if delete_session else 200)
        c.client.portal.call(release.set)
        assert sending.result(timeout=2)["code"] == "DELIVERED"
        assert c.identity not in c.service.codex.read().bindings
        view = c.client.get(f"/api/external-sessions/{c.identity}")
        if delete_session:
            assert view.status_code == 404
        else:
            assert view.json()["codex"]["status"] == "disabled"


def test_reply_references_stay_with_the_same_peer_and_chat(collaboration):
    c = collaboration
    sent = c.send()
    assert c.reply(session_id="other", reply_to=sent["message_id"])["code"] == "INVALID_REPLY"
    assert c.send(recipient="其他 Bot", delivery_id="bad", reply_to=sent["message_id"])["code"] == "INVALID_REPLY"
    second = c.call("get_context", chat="2" * 32)["structuredContent"]["identity"]
    assert c.reply(identity=second, reply_to=sent["message_id"])["code"] == "INVALID_REPLY"


def test_bot_and_web_observe_the_same_generic_conversation(collaboration):
    c = collaboration
    original = c.send()["message_id"]
    c.reply(reply_to=original)

    async def verify():
        messages = await c.service.messages("coordinator")
        assert messages[0][0].payload.requests_decision and not messages[1][0].payload.requests_decision
        shown = messages[0][1].for_model()
        assert shown["sender"] == {"kind": "external", "name": c.context["name"], "identity": c.identity}
        assert shown["text"] == "你好，来讨论一下需求。"
        assert "independent-verification" not in str(shown)
        events = await SqliteJournal(c.store.require("coordinator")).snapshot("coordinator")
        state = replay("coordinator", events).state
        view = project_conversation("coordinator", events, state.steps, session=session_view(state))
        external = [item for item in view.items if item.kind == "external_message"]
        assert [item.direction for item in external] == ["received", "sent"]
        assert all(item.peer == c.context["name"] for item in external)
        assert external[1].reply_to == original

    c.client.portal.call(verify)
    web = c.client.get("/api/external-sessions/" + c.identity).json()
    assert [item["text"] for item in web["messages"]] == ["你好，来讨论一下需求。", "收到，我们可以继续讨论。"]
    assert web["session"]["peers"] == [{"name": "协调员", "session_id": "coordinator", "deleted": False}]
