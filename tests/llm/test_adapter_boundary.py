import os
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from helperme.llm.api import LLMAuthenticationError
from helperme.llm.config import ModelConfig
from helperme.llm.types import InvalidLLMResponse


class _AsyncStream:
    def __init__(self, chunks):
        self._chunks = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration from None


def _chunk(content=None, reasoning=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=content, reasoning_content=reasoning)
            )
        ]
    )


class _Message:
    def __init__(self, data: dict[str, object]):
        self.data = data

    def model_dump(self, *, exclude_none: bool):
        return {
            key: value
            for key, value in self.data.items()
            if not exclude_none or value is not None
        }


class LiteLLMAdapterBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        from helperme.llm.adapter import LiteLLMAdapter

        self.adapter = object.__new__(LiteLLMAdapter)

    def test_keeps_unknown_assistant_message_fields(self):
        response = _Message({
            "role": "assistant",
            "content": "done",
            "tool_calls": None,
            "reasoning_content": "",
            "provider_specific_fields": {"signature": "abc"},
        })

        parsed = self.adapter._parse_response(response)

        self.assertEqual(parsed.content, "done")
        self.assertEqual(parsed.message_extensions, {
            "reasoning_content": "",
            "provider_specific_fields": {"signature": "abc"},
        })

    def test_rejects_non_array_tool_calls(self):
        with self.assertRaisesRegex(InvalidLLMResponse, "array|null"):
            self.adapter._parse_response(_Message({"content": "done", "tool_calls": {}}))

    def test_rejects_missing_tool_call_fields(self):
        with self.assertRaisesRegex(InvalidLLMResponse, "tool call fields"):
            self.adapter._parse_response(_Message({
                "content": "",
                "tool_calls": [{"id": "call-1"}],
            }))


class LiteLLMAdapterUsageTest(unittest.IsolatedAsyncioTestCase):
    async def test_reads_cached_prompt_tokens(self):
        from helperme.llm.adapter import LiteLLMAdapter

        adapter = object.__new__(LiteLLMAdapter)
        adapter._read_attachment = None
        completion = SimpleNamespace(
            choices=[SimpleNamespace(message=_Message({"content": "done"}))],
            usage=SimpleNamespace(
                prompt_tokens=120,
                completion_tokens=3,
                prompt_tokens_details=SimpleNamespace(cached_tokens=96),
            ),
        )
        adapter._litellm = SimpleNamespace(
            stream_chunk_builder=Mock(return_value=completion),
        )
        adapter._completion = AsyncMock(return_value=_AsyncStream((_chunk("done"),)))

        result = await adapter.chat([], "model")

        self.assertEqual(result.usage.cached_input_tokens, 96)
        self.assertEqual(result.usage.uncached_input_tokens, 24)

    async def test_authentication_failure_has_a_specific_error(self):
        from helperme.llm.adapter import LiteLLMAdapter

        class LiteLLMError(Exception):
            pass

        class OtherLiteLLMError(Exception):
            pass

        adapter = object.__new__(LiteLLMAdapter)
        adapter._read_attachment = None
        adapter._litellm = SimpleNamespace(
            ContextWindowExceededError=OtherLiteLLMError,
            AuthenticationError=LiteLLMError,
            PermissionDeniedError=OtherLiteLLMError,
            APIConnectionError=OtherLiteLLMError,
            Timeout=OtherLiteLLMError,
            RateLimitError=OtherLiteLLMError,
            InternalServerError=OtherLiteLLMError,
            ServiceUnavailableError=OtherLiteLLMError,
            APIError=OtherLiteLLMError,
            stream_chunk_builder=Mock(),
        )
        adapter._completion = AsyncMock(side_effect=LiteLLMError("invalid api key"))

        with self.assertRaisesRegex(LLMAuthenticationError, "invalid api key"):
            await adapter.chat([], "model")


class LiteLLMAdapterStreamingTest(unittest.IsolatedAsyncioTestCase):
    def _adapter(self, chunks, response):
        from helperme.llm.adapter import LiteLLMAdapter

        class LiteLLMError(Exception):
            pass

        adapter = object.__new__(LiteLLMAdapter)
        adapter._read_attachment = None
        adapter._completion = AsyncMock(return_value=_AsyncStream(chunks))
        adapter._litellm = SimpleNamespace(
            stream_chunk_builder=Mock(return_value=response),
            ContextWindowExceededError=LiteLLMError,
            AuthenticationError=LiteLLMError,
            PermissionDeniedError=LiteLLMError,
            APIConnectionError=LiteLLMError,
            Timeout=LiteLLMError,
            RateLimitError=LiteLLMError,
            InternalServerError=LiteLLMError,
            ServiceUnavailableError=LiteLLMError,
            APIError=LiteLLMError,
        )
        return adapter

    @staticmethod
    def _completion(message):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(
                prompt_tokens=4,
                completion_tokens=2,
                prompt_tokens_details=None,
            ),
        )

    async def test_emits_content_deltas_and_builds_one_final_response(self):
        chunks = (_chunk("hel"), _chunk(None), _chunk("lo"))
        completion = self._completion(_Message({"content": "hello"}))
        adapter = self._adapter(chunks, completion)
        emitted: list[str] = []

        result = await adapter.chat([], "model", on_content_delta=emitted.append)

        self.assertEqual(emitted, ["hel", "lo"])
        self.assertEqual(result.response.content, "hello")
        adapter._litellm.stream_chunk_builder.assert_called_once_with(
            list(chunks),
            messages=[],
        )

    async def test_emits_reasoning_deltas_without_mixing_them_into_content(self):
        chunks = (_chunk(reasoning="想"), _chunk("答"))
        completion = self._completion(
            _Message({"content": "答", "reasoning_content": "想"})
        )
        adapter = self._adapter(chunks, completion)
        content: list[str] = []
        reasoning: list[str] = []

        result = await adapter.chat(
            [],
            "model",
            on_content_delta=content.append,
            on_reasoning_delta=reasoning.append,
        )

        self.assertEqual(content, ["答"])
        self.assertEqual(reasoning, ["想"])
        self.assertEqual(result.response.content, "答")
        self.assertEqual(result.response.message_extensions["reasoning_content"], "想")

    async def test_tool_call_chunks_are_not_emitted_as_text(self):
        chunks = (_chunk(None),)
        completion = self._completion(_Message({
            "content": None,
            "tool_calls": [{
                "id": "call-1",
                "function": {"name": "read_file", "arguments": "{}"},
            }],
        }))
        adapter = self._adapter(chunks, completion)
        emitted: list[str] = []

        result = await adapter.chat([], "model", on_content_delta=emitted.append)

        self.assertEqual(emitted, [])
        self.assertEqual(result.response.calls[0].name, "read_file")

    async def test_rejects_content_that_differs_from_the_assembled_response(self):
        completion = self._completion(_Message({"content": "different"}))
        adapter = self._adapter((_chunk("shown"),), completion)

        with self.assertRaisesRegex(InvalidLLMResponse, "does not match"):
            await adapter.chat([], "model")

    async def test_content_callback_failure_is_not_wrapped(self):
        completion = self._completion(_Message({"content": "shown"}))
        adapter = self._adapter((_chunk("shown"),), completion)

        def fail(_content):
            raise RuntimeError("preview failed")

        with self.assertRaisesRegex(RuntimeError, "preview failed"):
            await adapter.chat([], "model", on_content_delta=fail)


class LiteLLMAdapterRequestTest(unittest.IsolatedAsyncioTestCase):
    def test_initializes_bundled_tokenizer_from_windows_line_endings(self):
        from helperme.llm.adapter import _initialize_bundled_tokenizers

        with TemporaryDirectory() as directory:
            package_root = Path(directory) / "litellm"
            tokenizer_root = package_root / "litellm_core_utils" / "tokenizers"
            tokenizer_root.mkdir(parents=True)
            resource = tokenizer_root / "encoding"
            resource.write_bytes(b"first\r\nsecond\r\n")
            expected = sha256(b"first\nsecond\n").hexdigest()
            spec = SimpleNamespace(submodule_search_locations=[str(package_root)])

            with (
                patch("helperme.llm.adapter.find_spec", return_value=spec),
                patch.dict(
                    "helperme.llm.adapter._BUNDLED_TIKTOKEN_RESOURCES",
                    {"encoding": expected},
                    clear=True,
                ),
            ):
                _initialize_bundled_tokenizers()

            self.assertEqual(resource.read_bytes(), b"first\nsecond\n")

    def test_passes_router_config_without_interpreting_it(self):
        config = ModelConfig(
            active="logical-model",
            router={
                "model_list": [{
                    "model_name": "logical-model",
                    "litellm_params": {"model": "openai/provider-model"},
                }],
                "routing_strategy": "least-busy",
            },
        )

        from helperme.llm.adapter import LiteLLMAdapter

        fake_litellm = SimpleNamespace(Router=Mock())
        initialized = False

        def initialize_tokenizers():
            nonlocal initialized
            initialized = True

        def import_litellm(name):
            self.assertTrue(initialized)
            self.assertEqual(os.environ["LITELLM_LOCAL_MODEL_COST_MAP"], "True")
            self.assertNotIn("CUSTOM_TIKTOKEN_CACHE_DIR", os.environ)
            self.assertEqual(name, "litellm")
            return fake_litellm

        with (
            patch.dict(
                os.environ,
                {
                    "LITELLM_LOCAL_MODEL_COST_MAP": "False",
                    "CUSTOM_TIKTOKEN_CACHE_DIR": "stale-cache",
                },
                clear=False,
            ),
            patch(
                "helperme.llm.adapter._initialize_bundled_tokenizers",
                side_effect=initialize_tokenizers,
            ),
            patch("helperme.llm.adapter.import_module", side_effect=import_litellm),
        ):
            LiteLLMAdapter(config)

        fake_litellm.Router.assert_called_once_with(**config.router)

    async def test_calls_in_process_router(self):
        from helperme.llm.adapter import LiteLLMAdapter

        adapter = object.__new__(LiteLLMAdapter)
        adapter._read_attachment = None
        adapter._router = SimpleNamespace(acompletion=AsyncMock(return_value=object()))

        await adapter._completion("logical-model", [], None)

        adapter._router.acompletion.assert_awaited_once_with(
            model="logical-model",
            messages=[],
            tools=None,
            tool_choice=None,
            stream=True,
            stream_options={"include_usage": True},
        )
