import asyncio
import json

import httpx
import pytest

from thinllm import (
    CodexResponsesClient, Endpoint, InvalidLLMResponse, LLMAuthenticationError,
    LLMContextLengthError, LLMProviderError, MissingProviderSetting, create_client, resolve_endpoint,
)


MODEL = "openai-codex/test-model"
ENDPOINT = Endpoint("openai-codex", "https://chatgpt.com/backend-api/codex", "token", False, "account")
REASONING = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"}
CALL = {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": '{"key":"answer"}'}


def completed(output=None):
    return {"type": "response.completed", "response": {
        "status": "completed",
        "output": output if output is not None else [{"type": "message", "content": [
            {"type": "output_text", "text": "OK"},
        ]}],
        "usage": {"input_tokens": 120, "output_tokens": 5, "input_tokens_details": {"cached_tokens": 96}},
    }}


def sse(*events):
    return b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)


def write_auth(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def auth_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    path = tmp_path / "auth.json"
    write_auth(path, {"auth_mode": "chatgpt", "tokens": {"access_token": "first", "account_id": "account"}})
    return path


def test_auth_resolution_reads_latest_login_without_modifying_it(auth_file, tmp_path):
    before = auth_file.read_bytes()
    endpoint = resolve_endpoint(MODEL, {"auth_file": ""})
    assert endpoint.api_key == "first"
    assert endpoint.account_id == "account"
    assert auth_file.read_bytes() == before
    write_auth(auth_file, {"tokens": {"access_token": "updated", "account_id": "other"}})
    assert resolve_endpoint(MODEL, {"auth_file": str(auth_file)}).api_key == "updated"
    with pytest.raises(MissingProviderSetting, match="thinllm auth codex"):
        resolve_endpoint(MODEL, {"auth_file": str(tmp_path / "missing.json")})
    write_auth(auth_file, {"auth_mode": "apikey", "OPENAI_API_KEY": "not-a-chatgpt-login"})
    with pytest.raises(MissingProviderSetting, match="ChatGPT"):
        resolve_endpoint(MODEL, {"auth_file": ""})
    auth_file.write_text("{", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        resolve_endpoint(MODEL, {"auth_file": ""})


def test_default_auth_location_and_explicit_path_are_independent(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr("thinllm.auth.Path.home", lambda: tmp_path)
    path = tmp_path / ".codex" / "auth.json"
    write_auth(path, {"tokens": {"access_token": "default", "account_id": "account"}})
    assert resolve_endpoint(MODEL, {"auth_file": ""}).api_key == "default"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "other"))
    assert resolve_endpoint(MODEL, {"auth_file": str(path)}).api_key == "default"


@pytest.mark.parametrize("auth", [[], {"tokens": []}, {"tokens": {"access_token": True, "account_id": "a"}}])
def test_damaged_auth_is_exposed_without_echoing_credentials(auth_file, auth):
    write_auth(auth_file, auth)
    with pytest.raises(ValueError):
        resolve_endpoint(MODEL, {"auth_file": ""})


def test_codex_stream_and_tool_history_use_responses_without_duplicating_content():
    async def run():
        requests = []
        responses = iter([
            sse({"type": "response.reasoning_summary_text.delta", "delta": "thinking"},
                {"type": "response.output_text.delta", "delta": "checking"},
                completed([REASONING, {"type": "message", "content": [{"type": "output_text", "text": "checking"}]}, CALL])),
            sse({"type": "response.output_text.delta", "delta": "OK"}, completed()),
        ])

        def handle(request):
            requests.append(request)
            return httpx.Response(200, content=next(responses))

        tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
        history = [{"role": "system", "content": "help"}, {"role": "user", "content": [
            {"type": "text", "text": "read"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA==", "detail": "low"}},
        ]}]
        content, reasoning = [], []
        async with create_client(ENDPOINT, transport=httpx.MockTransport(handle)) as client:
            assert isinstance(client, CodexResponsesClient)
            first = await client.chat(history, MODEL, tools, on_content_delta=content.append,
                                      on_reasoning_delta=reasoning.append, reasoning_effort="high")
            assert first.response.content == "checking"
            assert content == ["checking"] and reasoning == ["thinking"]
            assert first.usage.cached_input_tokens == 96
            call = first.response.calls[0]
            second = await client.chat([*history, {
                "role": "assistant", "content": first.response.content, **first.response.message_extensions,
                "tool_calls": [{"id": call.id, "function": {"name": call.name, "arguments": call.arguments}}],
            }, {"role": "tool", "tool_call_id": call.id, "content": "42"}], MODEL, tools)
        assert second.response.content == "OK"
        assert str(requests[0].url) == "https://chatgpt.com/backend-api/codex/responses"
        assert requests[0].headers["authorization"] == "Bearer token"
        assert requests[0].headers["chatgpt-account-id"] == "account"
        payload = json.loads(requests[0].content)
        assert payload["instructions"] == "help" and payload["model"] == "test-model"
        assert payload["store"] is False and payload["stream"] is True
        assert "messages" not in payload and "stream_options" not in payload
        assert payload["reasoning"] == {"effort": "high", "summary": "auto"}
        assert payload["tools"] == [{"type": "function", **tools[0]["function"]}]
        assert payload["input"][0]["content"][1] == {
            "type": "input_image", "image_url": "data:image/png;base64,AA==", "detail": "low",
        }
        replay = json.loads(requests[1].content)
        assert replay["input"][-4:] == [
            {key: value for key, value in REASONING.items() if key != "id"},
            {"role": "assistant", "content": [{"type": "output_text", "text": "checking"}]},
            CALL, {"type": "function_call_output", "call_id": "call_1", "output": "42"},
        ]

    asyncio.run(run())


@pytest.mark.parametrize("body", [
    sse({"type": "response.output_text.delta", "delta": "partial"}),
    sse({"type": "response.incomplete", "response": {"incomplete_details": {"reason": "max_output_tokens"}}}),
    sse({"type": "response.completed", "response": {"status": "completed", "output": []}}),
    sse({"type": "response.output_text.delta", "delta": 42}),
    sse({"type": "response.completed", "response": {
        "status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}],
        "usage": {"input_tokens": 1, "output_tokens": 1, "input_tokens_details": []},
    }}),
    b"data: {\n\n", b"data: [DONE]\n\n",
])
def test_incomplete_or_malformed_stream_is_never_a_success(body):
    async def run():
        async with create_client(ENDPOINT, transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))) as client:
            with pytest.raises(InvalidLLMResponse):
                await client.chat([], MODEL)

    asyncio.run(run())


@pytest.mark.parametrize(("status", "body", "error"), [
    (401, b'{"error":{"message":"expired"}}', LLMAuthenticationError),
    (200, sse({"type": "response.failed", "response": {"error": {
        "code": "context_length_exceeded", "message": "too long",
    }}}), LLMContextLengthError),
    (200, sse({"type": "error", "code": "server_error", "message": "failed"}), LLMProviderError),
])
def test_upstream_errors_keep_the_existing_error_contract(status, body, error):
    async def run():
        async with create_client(ENDPOINT, transport=httpx.MockTransport(lambda _: httpx.Response(status, content=body))) as client:
            with pytest.raises(error):
                await client.chat([], MODEL)

    asyncio.run(run())


def test_callback_errors_are_not_reclassified_as_upstream_corruption():
    async def run():
        error = TypeError("caller bug")

        def callback(_):
            raise error

        body = sse({"type": "response.output_text.delta", "delta": "hello"})
        async with create_client(ENDPOINT, transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))) as client:
            with pytest.raises(TypeError) as caught:
                await client.chat([], MODEL, on_content_delta=callback)
        assert caught.value is error

    asyncio.run(run())
