import asyncio
import json

import httpx
import pytest

from thinllm import (
    ResponsesClient, Endpoint, InvalidLLMResponse, LLMAuthenticationError,
    LLMContextLengthError, LLMProviderError, MissingProviderSetting, create_client, resolve_endpoint,
)


MODEL = "openai-codex/test-model"
ENDPOINT = Endpoint("openai-codex", "https://chatgpt.com/backend-api/codex", "token", False, "account")
REASONING = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"}
CALL = {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": '{"key":"answer"}'}


@pytest.fixture(params=["openai", "openai-codex"])
def responses_connection(request):
    if request.param == "openai-codex":
        return ENDPOINT, MODEL
    return resolve_endpoint("openai/gpt-6.1-terra", {"api_key": "existing-key"}), "openai/gpt-6.1-terra"


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


@pytest.mark.parametrize("searched", [False, True])
def test_openai_search_keeps_auto_choice_and_distinguishes_direct_answers(searched):
    output = [{"type": "message", "content": [{"type": "output_text", "text": "answer",
              "annotations": [{"type": "url_citation", "url": "https://example.com/", "title": "Source"}] if searched else []}]}]
    if searched:
        output.insert(0, {"type": "web_search_call", "action": {"type": "search",
                        "sources": [{"type": "url", "url": "https://example.com/"}]}})

    def handle(request):
        payload = json.loads(request.content)
        assert request.url == "https://api.openai.com/v1/responses"
        assert payload["tools"] == [{"type": "web_search"}]
        assert payload["tool_choice"] == "auto"
        assert payload["model"] == "gpt-6.1-terra"
        assert payload["include"] == ["web_search_call.action.sources"]
        return httpx.Response(200, content=sse(completed(output)))

    async def run():
        endpoint = resolve_endpoint("openai/gpt-6.1-terra", {"api_key": "existing-key"})
        async with ResponsesClient(endpoint, transport=httpx.MockTransport(handle)) as client:
            result = await client.web_search("question", "openai/gpt-6.1-terra")
        assert result == {"summary": "answer", "searched": searched,
                          "sources": [{"url": "https://example.com/", "title": "Source", "snippet": ""}] if searched else []}
    asyncio.run(run())


def write_auth(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def auth_file(tmp_path, monkeypatch):
    monkeypatch.setattr("thinllm.auth.Path.home", lambda: tmp_path)
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


@pytest.mark.parametrize("auth", [[], {"tokens": []}, {"tokens": {"access_token": True, "account_id": "a"}},
                                 {"auth_mode": "damaged", "tokens": {"access_token": "a", "account_id": "b"}}])
def test_damaged_auth_is_exposed_without_echoing_credentials(auth_file, auth):
    write_auth(auth_file, auth)
    with pytest.raises(ValueError):
        resolve_endpoint(MODEL, {"auth_file": ""})


def test_stream_and_tool_history_use_responses_without_losing_native_output(responses_connection):
    endpoint, model = responses_connection

    async def run():
        requests = []
        output = [{"type": "message", "role": "assistant", "phase": "commentary", "content": [
            {"type": "output_text", "text": "checking"},
        ]}, REASONING, CALL, {**CALL, "call_id": "call_2"}]
        responses = iter([
            sse({"type": "response.reasoning_summary_text.delta", "delta": "thinking"},
                {"type": "response.output_text.delta", "delta": "checking"},
                completed(output)),
            sse({"type": "response.output_text.delta", "delta": "OK"}, completed()),
        ])

        def handle(request):
            requests.append(request)
            return httpx.Response(200, content=next(responses))

        tools = [{"type": "function", "function": {"name": "lookup", "parameters": {
            "type": "object", "properties": {"key": {"type": "string"}, "optional": {"type": "string"}},
            "required": ["key"],
        }}}]
        history = [{"role": "system", "content": "help"}, {"role": "user", "content": [
            {"type": "text", "text": "read"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA==", "detail": "low"}},
        ]}]
        content, reasoning = [], []
        async with create_client(endpoint, transport=httpx.MockTransport(handle)) as client:
            assert isinstance(client, ResponsesClient)
            first = await client.chat(history, model, tools, on_content_delta=content.append,
                                      on_reasoning_delta=reasoning.append, reasoning_effort="high")
            assert first.response.content == "checking"
            assert content == ["checking"] and reasoning == ["thinking"]
            assert first.usage.cached_input_tokens == 96
            assert [call.id for call in first.response.calls] == ["call_1", "call_2"]
            assert first.response.message_extensions["responses_output"] == output
            second = await client.chat([*history, {
                "role": "assistant", "content": first.response.content, **first.response.message_extensions,
                "tool_calls": [{"id": "command_" + call.id, "function": {"name": call.name, "arguments": call.arguments}}
                               for call in first.response.calls],
            }, *[{"role": "tool", "tool_call_id": "command_" + call.id, "content": "42"}
                 for call in first.response.calls]], model, tools)
        assert second.response.content == "OK"
        assert str(requests[0].url) == endpoint.base_url + "/responses"
        assert requests[0].headers["authorization"] == "Bearer " + endpoint.api_key
        if endpoint.provider == "openai-codex":
            assert requests[0].headers["chatgpt-account-id"] == "account"
        else:
            assert "chatgpt-account-id" not in requests[0].headers
            assert "originator" not in requests[0].headers
        payload = json.loads(requests[0].content)
        assert payload["instructions"] == "help" and payload["model"] == model.partition("/")[2]
        assert payload["store"] is False and payload["stream"] is True
        assert "messages" not in payload and "stream_options" not in payload
        assert payload["reasoning"] == {"effort": "high", "summary": "auto"}
        assert payload["tools"] == [{"type": "function", "strict": False, **tools[0]["function"]}]
        assert payload["input"][0]["content"][1] == {
            "type": "input_image", "image_url": "data:image/png;base64,AA==", "detail": "low",
        }
        replay = json.loads(requests[1].content)
        assert "reasoning" not in replay
        assert replay["input"][1:] == [
            *output[:2],
            {**CALL, "call_id": "command_call_1"}, {**CALL, "call_id": "command_call_2"},
            {"type": "function_call_output", "call_id": "command_call_1", "output": "42"},
            {"type": "function_call_output", "call_id": "command_call_2", "output": "42"},
        ]

    asyncio.run(run())


def test_replayed_control_call_does_not_create_an_unpaired_tool_call(responses_connection):
    endpoint, model = responses_connection

    async def run():
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, content=sse(completed()))

        history = [{"role": "assistant", "content": "Waiting for confirmation.",
                    "responses_output": [REASONING, CALL]}]
        async with create_client(endpoint, transport=httpx.MockTransport(handle)) as client:
            await client.chat(history, model)
        assert requests[0]["input"] == [REASONING, {"role": "assistant", "content": [
            {"type": "output_text", "text": "Waiting for confirmation."},
        ]}]

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
def test_incomplete_or_malformed_stream_is_never_a_success(body, responses_connection):
    endpoint, model = responses_connection

    async def run():
        async with create_client(endpoint, transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))) as client:
            with pytest.raises(InvalidLLMResponse):
                await client.chat([], model)

    asyncio.run(run())


@pytest.mark.parametrize(("status", "body", "error"), [
    (401, b'{"error":{"message":"expired"}}', LLMAuthenticationError),
    (200, sse({"type": "response.failed", "response": {"error": {
        "code": "context_length_exceeded", "message": "too long",
    }}}), LLMContextLengthError),
    (200, sse({"type": "error", "code": "server_error", "message": "failed"}), LLMProviderError),
])
def test_upstream_errors_keep_the_existing_error_contract(status, body, error, responses_connection):
    endpoint, model = responses_connection

    async def run():
        async with create_client(endpoint, transport=httpx.MockTransport(lambda _: httpx.Response(status, content=body))) as client:
            with pytest.raises(error):
                await client.chat([], model)

    asyncio.run(run())


def test_callback_errors_are_not_reclassified_as_upstream_corruption(responses_connection):
    endpoint, model = responses_connection

    async def run():
        error = TypeError("caller bug")

        def callback(_):
            raise error

        body = sse({"type": "response.output_text.delta", "delta": "hello"})
        async with create_client(endpoint, transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))) as client:
            with pytest.raises(TypeError) as caught:
                await client.chat([], model, on_content_delta=callback)
        assert caught.value is error

    asyncio.run(run())
