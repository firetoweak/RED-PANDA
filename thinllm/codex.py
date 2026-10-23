"""Codex Responses wire adapter with per-request credential renewal.

Protocol reference: HKUDS/nanobot's OpenAICodexProvider and ResponsesBackend.
"""
from __future__ import annotations

import json

from thinllm.chat_completions import ChatCompletionsClient, _ToolCallParts
from thinllm.auth import CodexAuth
from thinllm.errors import InvalidLLMResponse
from thinllm.types import LLMUsage


class CodexResponsesClient(ChatCompletionsClient):
    """Reuse the streaming transport, retries and result contract for Codex."""

    _request_path = "responses"
    _done_marker = None
    _completion_label = "the response.completed event"

    def __init__(self, endpoint, **kwargs):
        super().__init__(endpoint, auth=CodexAuth(endpoint.auth_file) if endpoint.auth_file else None, **kwargs)

    def _payload(self, messages, model, tools, reasoning_effort) -> dict[str, object]:
        instructions = []
        items = []
        for message in messages:
            role = message["role"]
            content = message.get("content")
            if role in ("system", "developer"):
                instructions.append(content)
                continue
            if role == "tool":
                items.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                              "output": content})
                continue
            if role == "assistant":
                items.extend(message.get("codex_reasoning", []))
            if content:
                if isinstance(content, str):
                    content = [{"type": "output_text" if role == "assistant" else "input_text",
                                "text": content}]
                else:
                    content = [self._content_part(part, role) for part in content]
                items.append({"role": role, "content": content})
            for call in message.get("tool_calls", []):
                items.append({"type": "function_call", "call_id": call["id"],
                              "name": call["function"]["name"],
                              "arguments": call["function"]["arguments"]})
        payload = {
            **self._request_options,
            "model": model.partition("/")[2], "instructions": "\n\n".join(instructions),
            "input": items, "store": False, "stream": True,
            "include": ["reasoning.encrypted_content"],
        }
        if tools:
            payload["tools"] = [{"type": "function", **tool["function"]} for tool in tools]
            payload["tool_choice"] = "auto"
            payload["parallel_tool_calls"] = True
        if reasoning_effort is not None:
            payload["reasoning"] = {"effort": reasoning_effort, "summary": "auto"}
        return payload

    @staticmethod
    def _content_part(part, role):
        if part["type"] == "text":
            return {"type": "output_text" if role == "assistant" else "input_text", "text": part["text"]}
        if part["type"] == "image_url":
            return {
                "type": "input_image", "image_url": part["image_url"]["url"],
                **({"detail": part["image_url"]["detail"]} if "detail" in part["image_url"] else {}),
            }
        raise ValueError(f"Unsupported Codex content part: {part['type']}")

    async def _consume(self, data, stream, on_content_delta, on_reasoning_delta) -> None:
        content_delta = reasoning_delta = None
        try:
            event = json.loads(data)
            kind = event["type"]
            if kind == "response.output_text.delta":
                content_delta = self._text(event["delta"])
            elif kind == "response.reasoning_summary_text.delta":
                reasoning_delta = self._text(event["delta"])
                stream.extensions["reasoning_content"] = stream.extensions.get("reasoning_content", "") + reasoning_delta
            elif kind == "response.completed":
                response = event["response"]
                if response["status"] != "completed":
                    raise InvalidLLMResponse("invalid_llm_response", "Codex response did not complete")
                self._complete(response, stream)
            elif kind in ("response.failed", "response.incomplete"):
                error = event["response"].get("error")
                if error is not None:
                    self._raise_stream_error(error)
                raise InvalidLLMResponse("invalid_llm_response", f"Codex {kind}")
            elif kind == "error":
                self._raise_stream_error(event.get("error", event))
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as error:
            raise InvalidLLMResponse("invalid_llm_response", "Malformed Codex stream event") from error
        if content_delta is not None:
            await self._emit(on_content_delta, content_delta)
        if reasoning_delta is not None:
            await self._emit(on_reasoning_delta, reasoning_delta)

    @staticmethod
    def _text(value):
        if type(value) is not str:
            raise InvalidLLMResponse("invalid_llm_response", "Codex text must be a string")
        return value

    def _complete(self, response, stream):
        reasoning = []
        for index, item in enumerate(response["output"]):
            kind = item["type"]
            if kind == "message":
                stream.content_parts.extend(self._text(part["text"]) for part in item["content"]
                                            if part["type"] == "output_text")
            elif kind == "function_call":
                stream.tool_calls[index] = _ToolCallParts(item["call_id"], item["name"], item["arguments"])
            elif kind == "reasoning":
                reasoning.append({key: value for key, value in item.items() if key != "id"})
        if reasoning:
            stream.extensions["codex_reasoning"] = reasoning
        usage = response["usage"]
        details = usage.get("input_tokens_details")
        if details is None:
            details = {}
        stream.usage = LLMUsage(usage["input_tokens"], usage["output_tokens"], details.get("cached_tokens", 0))
        stream.received_done = True


def create_client(endpoint, **kwargs):
    """Select the built-in wire protocol without involving product code."""
    client = CodexResponsesClient if endpoint.provider == "openai-codex" else ChatCompletionsClient
    return client(endpoint, **kwargs)
