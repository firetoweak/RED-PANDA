import asyncio
import json

import httpx
import pytest

from thinllm import InvalidLLMResponse, LLMAuthenticationError, LLMProviderError, LLMTransientError, list_codex_models
from thinllm import list_models


def model(slug="MixedCase-ID", **overrides):
    return {"slug": slug, "display_name": "Display name", "visibility": "list", "context_window": 500000,
            "default_reasoning_level": "low", "supported_reasoning_levels": [{"effort": "low", "description": "Light"},
                                                                           {"effort": "high", "description": "Deep"}],
            "base_instructions": "must not be exposed", **overrides}


@pytest.fixture
def auth_file(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"tokens": {"access_token": "private-access", "account_id": "account"}}), encoding="utf-8")
    return path


def test_lists_account_models_without_hidden_entries_or_prompt_metadata(auth_file):
    before = auth_file.read_bytes()
    requests = []

    def handle(request):
        requests.append(request)
        assert str(request.url).startswith("https://chatgpt.com/backend-api/codex/models?")
        assert request.url.params["client_version"]
        assert request.headers["Authorization"] == "Bearer private-access"
        assert request.headers["ChatGPT-Account-Id"] == "account"
        return httpx.Response(200, json={"models": [model(), model("hidden", visibility="hide"),
                                                   model("unknown-capacity", context_window=None,
                                                         supported_reasoning_levels=[], default_reasoning_level=None)]})

    result = asyncio.run(list_codex_models({"auth_file": str(auth_file)}, transport=httpx.MockTransport(handle)))
    assert len(requests) == 1
    assert result == [{"slug": "MixedCase-ID", "display_name": "Display name", "context_window": 500000,
                       "default_reasoning_level": "low", "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}]},
                      {"slug": "unknown-capacity", "display_name": "Display name", "context_window": None,
                       "default_reasoning_level": None, "supported_reasoning_levels": []}]
    assert auth_file.read_bytes() == before


@pytest.mark.parametrize(("status", "error"), [(401, LLMAuthenticationError), (403, LLMAuthenticationError),
                                              (429, LLMTransientError), (503, LLMTransientError), (400, LLMProviderError)])
def test_provider_failure_is_not_an_empty_catalog_and_does_not_echo_response(auth_file, status, error):
    transport = httpx.MockTransport(lambda _: httpx.Response(status, text="private-response"))
    with pytest.raises(error) as caught:
        asyncio.run(list_codex_models({"auth_file": str(auth_file)}, transport=transport))
    assert "private-response" not in str(caught.value)


@pytest.mark.parametrize("payload", [{}, {"models": {}}, {"models": [model(context_window=True)]},
                                     {"models": [model(supported_reasoning_levels=[{"effort": 1}])]}])
def test_malformed_catalog_has_a_protocol_error(auth_file, payload):
    with pytest.raises(InvalidLLMResponse, match="模型列表格式"):
        asyncio.run(list_codex_models({"auth_file": str(auth_file)}, transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=payload))))


def test_new_request_reads_new_credentials_and_catalog_without_local_cache(auth_file):
    tokens = []

    def handle(request):
        tokens.append(request.headers["authorization"])
        return httpx.Response(200, json={"models": [model(f"model-{len(tokens)}")]})

    async def run():
        settings = {"auth_file": str(auth_file)}
        transport = httpx.MockTransport(handle)
        first = await list_codex_models(settings, transport=transport)
        auth_file.write_text(json.dumps({"tokens": {"access_token": "rotated", "account_id": "other-account"}}), encoding="utf-8")
        second = await list_codex_models(settings, transport=transport)
        assert first[0]["slug"] == "model-1" and second[0]["slug"] == "model-2"

    asyncio.run(run())
    assert tokens == ["Bearer private-access", "Bearer rotated"]


def test_transport_errors_are_transient_and_corrupt_credentials_propagate(auth_file):
    def fail(request):
        raise httpx.ConnectError("network unavailable", request=request)

    with pytest.raises(LLMTransientError):
        asyncio.run(list_codex_models({"auth_file": str(auth_file)}, transport=httpx.MockTransport(fail)))
    auth_file.write_text("{", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        asyncio.run(list_codex_models({"auth_file": str(auth_file)}, transport=httpx.MockTransport(fail)))


@pytest.mark.parametrize(("provider", "entry", "levels", "default"), [
    ("deepseek", {"id": "deepseek-flash", "name": "DeepSeek Flash", "context_window": 1048576,
                  "effort": {"supported_levels": ["low", "high", "max"], "default_level": "high"}},
     ["low", "high", "max"], "high"),
    ("stepfun", {"id": "step-5-preview", "reasoning_effort_support_list": ["low", "medium", "high"]},
     ["low", "medium", "high"], None),
    ("openai", {"id": "gpt-6.1-sol"}, ["low", "medium", "high", "xhigh", "max"], "medium"),
    ("qwen", {"id": "unknown-new-model"}, [], None),
])
def test_native_model_capabilities_are_not_replaced_by_provider_wide_tiers(provider, entry, levels, default):
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"data": [entry]}))
    result = asyncio.run(list_models(provider, {"api_key": "private"}, transport=transport))
    assert result[0]["reasoning_levels"] == levels
    assert result[0]["default_reasoning_effort"] == default
    assert result[0]["model"] == provider + "/" + entry["id"]
    assert "private" not in repr(result)


def test_openai_catalog_excludes_non_conversation_and_retired_models():
    entries = [{"id": "gpt-6.1-terra"}, {"id": "gpt-image-2"}, {"id": "text-embedding-3-small"},
               {"id": "gpt-4o-mini-transcribe"}, {"id": "gpt-5-search-api"},
               {"id": "gpt-4o", "shutdown_date": "2020-01-01"}]
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"data": entries}))
    result = asyncio.run(list_models("openai", {"api_key": "private"}, transport=transport))
    assert [item["model"] for item in result] == ["openai/gpt-6.1-terra"]


def test_invalid_native_levels_are_a_catalog_protocol_error():
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"data": [
        {"id": "deepseek-flash", "effort": {"supported_levels": [1]}}]}))
    with pytest.raises(InvalidLLMResponse):
        asyncio.run(list_models("deepseek", {"api_key": "private"}, transport=transport))
