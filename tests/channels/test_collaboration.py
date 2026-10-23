"""Verification contracts at the external MCP and human Web boundaries."""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from redpanda.assistant.agents import AgentCreated, fact_arguments
from redpanda.assistant.conversations import project_conversation
from redpanda.assistant.external_messages import REPLY_EXTERNAL, ReplyExternal
from redpanda.assistant.host.external_messages import ExternalMessages
from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.assistant.sessions import session_view
from redpanda.channels.web.app import create_web_app
from redpanda.channels.web.channel import WebChannel
from redpanda.paths import RedPandaHome
from redpanda.runtime import AgentRuntime, SqliteJournal, replay


IDENTITY = "1" * 32
REQUEST = dict(identity=IDENTITY, recipient="协调员", text="输入不能为空白；合法输入应成功。请独立验收，不采信开发者自测。",
               delivery_id="version-a", revision="a" * 40, reference="https://example.com/pr/1")


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
    first, token = service.connections.create()
    _, other_token = service.connections.create()
    web = WebChannel(host, None)
    app = create_web_app(channel=web, external_messages=service)
    deliveries = []

    async def deliver(operation, session_id, arguments):
        assert operation == "fact"
        deliveries.append(arguments)
        await AgentRuntime(SqliteJournal(store.require(session_id)), None, {}).receive_domain_fact(session_id, **arguments)

    with patch.object(host, "request", side_effect=deliver):
        with TestClient(app, base_url="http://127.0.0.1:8876") as client:
            human = client.portal.call(web.connect)

            def call(name, arguments=None, credential=token):
                response = client.post("/mcp/", headers={"Authorization": "Bearer " + credential,
                    "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": {} if arguments is None else arguments}})
                assert response.status_code == 200, response.text
                return response.json()["result"]

            def reply(request_id, command="report-1", session_id="coordinator", kind="report", **changes):
                fields = dict(kind=kind, verdict="passed" if kind == "report" else None,
                    tested_revision="a" * 40 if kind == "report" else None,
                    evidence=["evidence/validation.png"] if kind == "report" else [],
                    reply_to=request_id, text="独立验证反馈")
                fields.update(changes)
                args = ReplyExternal(**fields).model_dump()
                return client.portal.call(host._route, REPLY_EXTERNAL, session_id, {"command_id": command, **args})

            def read(request_id, identity=IDENTITY, credential=token, **options):
                return call("read_feedback", dict(identity=identity, request_id=request_id, wait_seconds=0, **options),
                            credential)["structuredContent"]

            yield SimpleNamespace(host=host, service=service, store=store, client=client,
                call=call, read=read, reply=reply, connection=first, token=token, other_token=other_token,
                deliveries=deliveries, human_connection=human.connection_id)
            client.portal.call(web.disconnect, human)
            client.portal.call(host.close)


def test_web_creates_global_access_without_names_or_a_fixed_recipient(collaboration):
    c = collaboration
    path = "/api/external-connections"
    assert c.client.post(path, json={"connection_id": "inactive"}).status_code == 409
    assert c.client.post(path, json={"connection_id": c.human_connection, "name": "用户不必取名"}).status_code == 422
    created = c.client.post(path, json={"connection_id": c.human_connection})
    assert created.status_code == 201 and created.headers["cache-control"] == "no-store"
    credential = created.json()
    assert credential["url"] == "http://127.0.0.1:8876/mcp/"
    entries = c.client.get(path).json()
    assert credential["connection"] in entries and all(set(item) == {"connection_id"} for item in entries)
    assert credential["token"] not in str(entries)
    context = c.call("get_context", credential=credential["token"])["structuredContent"]
    assert {item["name"] for item in context["colleagues"]} == {"协调员", "其他 Bot"}
    external_id = credential["connection"]["connection_id"]
    assert c.client.request("DELETE", f"{path}/{external_id}", json={"connection_id": c.human_connection}).status_code == 204
    assert c.client.request("DELETE", f"{path}/{external_id}", json={"connection_id": c.human_connection}).status_code == 404
    assert c.client.post("/mcp/", headers={"Authorization": "Bearer " + credential["token"]}, json={}).status_code == 401


def test_only_current_access_and_chat_can_read_the_request(collaboration):
    c = collaboration
    assert c.client.post("/mcp/", json={}).status_code == 401
    assert c.client.post("/mcp/", headers={"Authorization": "Bearer invalid"}, json={}).status_code == 401
    context = c.call("get_context")["structuredContent"]
    assert context["name"] == "编码会话-" + context["identity"][:8]
    assert all(set(item) == {"name"} for item in context["colleagues"])
    sent = c.call("request_verification", REQUEST)["structuredContent"]
    assert sent["code"] == "ACCEPTED"
    c.reply(sent["request_id"])
    history = c.read(sent["request_id"])
    assert len(history["feedback"]) == 1
    assert "PRIVATE HUMAN CONTENT" not in str(history) + str(context)
    assert "token_hash" not in str(history) + str(context)
    assert c.read(sent["request_id"], credential=c.other_token)["code"] == "REQUEST_NOT_FOUND"
    assert c.read(sent["request_id"], identity="2" * 32)["code"] == "REQUEST_NOT_FOUND"


def test_two_automatic_identities_and_their_version_requests_stay_distinct(collaboration):
    c = collaboration
    first = c.call("get_context")["structuredContent"]
    second = c.call("get_context")["structuredContent"]
    assert first["identity"] != second["identity"] and first["name"] != second["name"]
    assert c.call("get_context", {"identity": first["identity"]})["structuredContent"]["name"] == first["name"]
    one = c.call("request_verification", dict(REQUEST, identity=first["identity"]))["structuredContent"]
    two = c.call("request_verification", dict(REQUEST, identity=second["identity"]))["structuredContent"]
    assert one["request_id"] != two["request_id"]
    c.reply(two["request_id"], "second")
    c.reply(one["request_id"], "first")
    assert len(c.read(one["request_id"], identity=first["identity"])["feedback"]) == 1
    assert c.read(two["request_id"], identity=first["identity"])["code"] == "REQUEST_NOT_FOUND"


def test_retries_are_idempotent_and_new_versions_link_to_the_original_request(collaboration):
    c = collaboration
    sent = c.call("request_verification", REQUEST)["structuredContent"]
    assert c.call("request_verification", REQUEST)["structuredContent"] == sent
    assert len(c.deliveries) == 1
    assert c.call("request_verification", dict(REQUEST, text="changed"))["structuredContent"]["code"] == "DELIVERY_CONFLICT"
    assert c.call("request_verification", dict(REQUEST, recipient="其他 Bot"))["structuredContent"]["code"] == "DELIVERY_CONFLICT"
    first = c.reply(sent["request_id"])
    assert c.reply(sent["request_id"]) == first
    revised = dict(REQUEST, recipient=None, revision="b" * 40, delivery_id="version-b", previous_request_id=sent["request_id"])
    new = c.call("request_verification", revised)["structuredContent"]
    assert new["request_id"] != sent["request_id"] and new["recipient"] == "协调员"
    assert c.read(new["request_id"])["feedback"] == []
    c.reply(new["request_id"], "retest", tested_revision="b" * 40)
    history = c.read(new["request_id"])
    assert history["request"]["previous_request_id"] == sent["request_id"]
    assert history["request"]["revision"] == history["feedback"][0]["feedback"]["tested_revision"] == "b" * 40
    assert c.read(sent["request_id"])["feedback"][0]["revision"] == "a" * 40


@pytest.mark.parametrize("tool,arguments", [
    ("request_verification", dict(REQUEST, revision="main")),
    ("request_verification", dict(REQUEST, text=" ")),
    ("request_verification", {key: value for key, value in REQUEST.items() if key != "identity"}),
    ("request_verification", {key: value for key, value in REQUEST.items() if key != "revision"}),
    ("request_verification", {key: value for key, value in REQUEST.items() if key != "reference"}),
    ("request_verification", dict(REQUEST, recipient=None)),
    ("request_verification", dict(REQUEST, sender="user")),
    ("read_feedback", {"identity": IDENTITY}),
    ("read_feedback", {"identity": IDENTITY, "request_id": "unknown", "after": -1}),
    ("read_feedback", {"identity": IDENTITY, "request_id": "unknown", "wait_seconds": 51}),
    ("read_feedback", {"identity": IDENTITY, "request_id": "unknown", "after": True}),
    ("get_context", {"identity": " "}),
])
def test_invalid_handoff_is_rejected_before_delivery(collaboration, tool, arguments):
    assert collaboration.call(tool, arguments)["isError"]
    assert collaboration.deliveries == []


def test_progress_question_and_report_remain_distinct_and_cursor_survives_restart(collaboration):
    c = collaboration
    request_id = c.call("request_verification", REQUEST)["structuredContent"]["request_id"]
    c.reply(request_id, "working", kind="progress", text="正在验证")
    c.reply(request_id, "question", kind="question", text="请确认预期行为")
    c.reply(request_id, "report", verdict="issues_found", text="复现步骤：空白输入被接纳。预期应报错。")
    first = c.read(request_id, limit=2)
    assert [item["feedback"]["kind"] for item in first["feedback"]] == ["progress", "question"]
    assert first["has_more"] and first["next_cursor"] == 2
    restarted = ExternalMessages(c.host)
    remaining = c.client.portal.call(restarted.read, c.connection, IDENTITY, request_id, 2, 50, 0)
    assert len(remaining["feedback"]) == 1 and remaining["next_cursor"] == 3
    assert remaining["feedback"][0]["feedback"]["verdict"] == "issues_found"
    assert c.read(request_id, after=99)["code"] == "INVALID_CURSOR"


def test_waiting_reader_receives_feedback_and_revocation_interrupts_wait(collaboration):
    c = collaboration
    request_id = c.call("request_verification", REQUEST)["structuredContent"]["request_id"]
    waiting = c.client.portal.start_task_soon(c.service.read, c.connection, IDENTITY, request_id, 0, 50, 2)
    c.reply(request_id)
    assert waiting.result(timeout=3)["feedback"][0]["reply_to"] == request_id
    waiting = c.client.portal.start_task_soon(c.service.read, c.connection, IDENTITY, request_id, 1, 50, 2)
    c.client.portal.call(c.service.revoke_connection, c.connection.connection_id)
    assert waiting.result(timeout=3)["code"] == "CONNECTION_REVOKED"
    assert c.reply(request_id, "after-revoked")["code"] == "CONNECTION_REVOKED"


def test_request_and_report_are_visible_with_their_distinct_versions(collaboration):
    c = collaboration
    request_id = c.call("request_verification", REQUEST)["structuredContent"]["request_id"]
    c.reply(request_id, tested_revision="b" * 40)

    async def verify():
        messages = await c.service.messages("coordinator")
        assert messages[0][0].payload.requests_decision and not messages[1][0].payload.requests_decision
        assert "builtin:independent-verification" in messages[0][1].for_model()["notice"]
        events = await SqliteJournal(c.store.require("coordinator")).snapshot("coordinator")
        state = replay("coordinator", events).state
        view = project_conversation("coordinator", events, state.steps, session=session_view(state))
        external = [item for item in view.items if item.kind == "external_message"]
        assert [item.direction for item in external] == ["received", "sent"]
        assert all(item.peer == "编码会话-11111111" for item in external)
        assert external[1].revision == "a" * 40 and external[1].feedback.tested_revision == "b" * 40

    c.client.portal.call(verify)


def test_other_bot_cannot_reply_and_unknown_previous_requests_are_rejected(collaboration):
    c = collaboration
    request_id = c.call("request_verification", REQUEST)["structuredContent"]["request_id"]
    assert c.reply(request_id, session_id="other")["code"] == "INVALID_REPLY"
    invalid = dict(REQUEST, delivery_id="new", previous_request_id="unknown")
    assert c.call("request_verification", invalid)["structuredContent"]["code"] == "REQUEST_NOT_FOUND"
