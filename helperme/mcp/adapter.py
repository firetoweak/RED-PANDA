from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from typing import Any, Mapping

from mcp.types import (
    AudioContent,
    CallToolResult,
    EmbeddedResource,
    ImageContent,
    ResourceLink,
    TextContent,
)

from helperme.tools.spec import JsonSchemaParameters, ToolSpec
from helperme.mcp.toolsets import ToolsetLoadError
from jsonschema.exceptions import SchemaError, ValidationError
from jsonschema.validators import validator_for
from helperme.mcp.models import sanitize_error_summary


_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_-]+")
_MAX_TOOL_NAME_LENGTH = 64
_BINARY_BLOCK_TYPES = frozenset({"image", "audio"})


def encode_tool_name(server_id: str, tool_name: str) -> str:
    """稳定编码跨 Server 的扁平工具名。"""
    raw = f"mcp__{server_id}__{tool_name}"
    cleaned = _SAFE_NAME.sub("_", raw)
    if len(cleaned) <= _MAX_TOOL_NAME_LENGTH:
        return cleaned
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    keep = _MAX_TOOL_NAME_LENGTH - 9
    return f"{cleaned[:keep]}_{digest}"


def parse_toolset_id(toolset_id: str) -> str:
    prefix = "mcp:"
    if not toolset_id.startswith(prefix):
        raise ToolsetLoadError(
            "TOOLSET_NOT_FOUND",
            f"Toolset {toolset_id} not found",
            data={"toolset_id": toolset_id},
        )
    server_id = toolset_id[len(prefix) :]
    if not server_id:
        raise ToolsetLoadError(
            "TOOLSET_NOT_FOUND",
            f"Toolset {toolset_id} not found",
            data={"toolset_id": toolset_id},
        )
    return server_id


def build_parameters(tool_name: str, input_schema: Mapping[str, Any]) -> JsonSchemaParameters:
    try:
        return JsonSchemaParameters(input_schema)
    except (SchemaError, TypeError, ValueError) as exc:
        raise ToolsetLoadError(
            "MCP_INVALID_TOOL_SCHEMA",
            f"工具 {tool_name} 的 inputSchema 非法: {exc}",
            hint="请修复 MCP Server 的工具 Schema 后重试 load_toolset。",
            data={"tool_name": tool_name},
        ) from exc


def build_output_validator(
    tool_name: str,
    output_schema: Mapping[str, Any] | None,
) -> Any | None:
    if output_schema is None:
        return None
    schema = deepcopy(dict(output_schema))
    try:
        validator_class = validator_for(schema)
        validator_class.check_schema(schema)
        return validator_class(schema)
    except (SchemaError, TypeError, ValueError) as exc:
        raise ToolsetLoadError(
            "MCP_INVALID_TOOL_SCHEMA",
            f"工具 {tool_name} 的 outputSchema 非法: {exc}",
            hint="请修复 MCP Server 的工具 Schema 后重试 load_toolset。",
            data={"tool_name": tool_name},
        ) from exc


def adapt_call_result(
    result: CallToolResult,
    *,
    output_validator: Any | None = None,
    secret_values: tuple[str, ...] = (),
) -> dict[str, Any]:
    content = [
        # 二进制 base64 不是文本，字符串替换会破坏原始字节。
        block
        if block.get("type") in _BINARY_BLOCK_TYPES
        else _redact_secrets(block, secret_values)
        for block in (_serialize_content_block(item) for item in result.content)
    ]
    structured = _redact_secrets(result.structured_content, secret_values)
    meta = _redact_secrets(result.meta, secret_values)

    if result.is_error:
        return {
            "ok": False,
            "code": "MCP_TOOL_ERROR",
            "data": {
                "mcp": {
                    "content": content,
                    "structured_content": structured,
                    "meta": meta,
                }
            },
            "error": _text_from_content(content) or "MCP tool reported an error",
            "hint": "可根据服务端返回修正参数后重试。",
        }

    if output_validator is not None:
        if structured is None:
            return {
                "ok": False,
                "code": "MCP_INVALID_TOOL_RESULT",
                "data": {
                    "mcp": {
                        "content": content,
                        "structured_content": None,
                        "meta": meta,
                    }
                },
                "error": "工具声明了 outputSchema，但未返回 structuredContent",
                "hint": "请检查 MCP Server 返回值。",
            }
        try:
            output_validator.validate(result.structured_content)
        except ValidationError as exc:
            return {
                "ok": False,
                "code": "MCP_INVALID_TOOL_RESULT",
                "data": {
                    "mcp": {
                        "content": content,
                        "structured_content": structured,
                        "meta": meta,
                    }
                },
                "error": sanitize_error_summary(
                    f"structuredContent 不符合 outputSchema: {exc}",
                    secret_values=secret_values,
                ),
                "hint": "请检查 MCP Server 返回值或 outputSchema。",
            }

    return {
        "ok": True,
        "code": "MCP_TOOL_OK",
        "data": {
            "mcp": {
                "content": content,
                "structured_content": structured,
                "meta": meta,
            }
        },
    }


def _redact_secrets(value: Any, secret_values: tuple[str, ...]) -> Any:
    secrets = tuple(
        sorted(
            (secret for secret in secret_values if secret),
            key=len,
            reverse=True,
        )
    )
    if not secrets:
        return value

    def redact(item: Any) -> Any:
        if isinstance(item, str):
            for secret in secrets:
                item = item.replace(secret, "***")
            return item
        if isinstance(item, dict):
            return {
                redact(key): redact(child)
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [redact(child) for child in item]
        if isinstance(item, tuple):
            return tuple(redact(child) for child in item)
        return item

    return redact(value)


def adapt_transport_error(
    exc: BaseException,
    *,
    error_summary: str | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "code": "MCP_TRANSPORT_ERROR",
        "data": {},
        "error": error_summary or sanitize_error_summary(
            str(exc) or exc.__class__.__name__
        ),
        "hint": "检查 Server 是否可用、地址/命令是否正确，稍后重试。",
    }


def input_required_unsupported() -> dict[str, Any]:
    return {
        "ok": False,
        "code": "MCP_INPUT_REQUIRED_UNSUPPORTED",
        "data": {},
        "error": "该工具需要在调用中途再次请求输入，当前不支持",
        "hint": "请改用无需中途交互的工具。",
    }


def _serialize_content_block(block: Any) -> dict[str, Any]:
    if isinstance(block, TextContent):
        return {"type": "text", "text": block.text}
    if isinstance(block, ImageContent):
        return {
            "type": "image",
            "mimeType": block.mime_type,
            "data": block.data,
        }
    if isinstance(block, AudioContent):
        return {
            "type": "audio",
            "mimeType": block.mime_type,
            "data": block.data,
        }
    if isinstance(block, ResourceLink):
        return {
            "type": "resource_link",
            "name": block.name,
            "uri": str(block.uri),
            "description": block.description,
            "mimeType": block.mime_type,
        }
    if isinstance(block, EmbeddedResource):
        resource = block.resource
        payload: dict[str, Any] = {
            "type": "resource",
            "uri": str(resource.uri),
            "mimeType": getattr(resource, "mime_type", None),
        }
        if hasattr(resource, "text"):
            payload["text"] = resource.text
        if hasattr(resource, "blob"):
            payload["blob"] = resource.blob
        return payload
    if hasattr(block, "model_dump"):
        return block.model_dump(mode="json", by_alias=True)
    return {"type": "unknown", "repr": repr(block)}


def _text_from_content(content: list[dict[str, Any]]) -> str:
    texts = [
        item["text"]
        for item in content
        if item.get("type") == "text" and item.get("text")
    ]
    return "\n".join(texts)


def ensure_unique_encoded_names(specs: list[ToolSpec]) -> None:
    names = [spec.name for spec in specs]
    if len(names) != len(set(names)):
        raise ToolsetLoadError(
            "MCP_TOOL_NAME_CONFLICT",
            "该服务暴露的工具名互相冲突，无法加载",
            hint="这是服务端的问题，重试不会改变；如实告诉用户该 Toolset 当前不可用。",
            data={"names": names},
        )
