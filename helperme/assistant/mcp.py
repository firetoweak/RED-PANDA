from __future__ import annotations

from base64 import b64decode
from binascii import Error as Base64Error
from collections.abc import Mapping

from helperme.assistant.attachments import AttachmentRejected, AttachmentStore
from helperme.assistant.toolsets import (
    LoadedTool,
    LoadedToolSnapshot,
    ToolsetDescriptor,
    ToolsetLoadError,
)
from helperme.assistant.tool_results import runtime_tool_result
from helperme.mcp.toolsets import ToolsetLoadError as ProviderLoadError
from helperme.mcp.adapter import parse_toolset_id


class McpToolsetAdapter:
    """Translate the MCP catalog to the Assistant Toolset port."""

    def __init__(self, mcp, attachments: AttachmentStore, *, read_only_only: bool = False) -> None:
        self._provider = mcp.toolset_provider
        self._attachments = attachments
        self._read_only_only = read_only_only

    def descriptors(self) -> tuple[ToolsetDescriptor, ...]:
        return tuple(
            ToolsetDescriptor(
                item.id,
                item.description,
                item.revision,
            )
            for item in self._provider.descriptors(read_only_only=self._read_only_only)
        )

    def handles(self, toolset_id: str) -> bool:
        try:
            parse_toolset_id(toolset_id)
        except ProviderLoadError:
            return False
        return True

    async def load(
        self,
        toolset_id: str,
        revision: int,
    ) -> tuple[LoadedTool, ...]:
        try:
            current = {item.id: item for item in self.descriptors()}.get(toolset_id)
            if current is None or current.revision != revision:
                raise ToolsetLoadError(
                    "TOOLSET_REVISION_UNAVAILABLE",
                    f"Toolset {toolset_id} 在当前能力目录生成后已变化",
                    hint="按最新能力目录中的 Toolset 重新 load_toolset。",
                    data={
                        "toolset_id": toolset_id,
                        "expected_revision": revision,
                        "available_revision": (
                            None if current is None else current.revision
                        ),
                    },
                )
            discovered = await self._provider.discover_tools(toolset_id)
        except ProviderLoadError as exc:
            raise ToolsetLoadError(
                exc.code,
                exc.message,
                hint=exc.hint,
                data=exc.data,
            ) from exc
        return tuple(
            _loaded_from_spec(
                item.spec,
                self._attachments,
                provider_data=item.provider_data,
            )
            for item in discovered
        )

    def restore(
        self,
        toolset_id: str,
        revision: int,
        tools: tuple[LoadedToolSnapshot, ...],
    ) -> tuple[LoadedTool, ...]:
        return tuple(
            _loaded_from_spec(
                self._provider.restore_spec(
                    toolset_id=toolset_id,
                    revision=revision,
                    name=tool.name,
                    description=tool.description,
                    parameters=tool.parameters,
                    requires_authorization=tool.requires_authorization,
                    provider_data=tool.provider_data,
                ),
                self._attachments,
                provider_data=tool.provider_data,
            )
            for tool in tools
        )


def extract_images(result: object, store: AttachmentStore) -> object:
    """把 MCP content 里的图片块换成附件引用。

    只认 MCP 结果结构，不递归猜测普通 JSON 的含义。必须早于文本外置执行，
    否则 base64 会被当成长文本写进文本 Artifact。
    """

    if type(result) is not dict:
        return result
    data = result.get("data")
    if type(data) is not dict or type(data.get("mcp")) is not dict:
        return result
    blocks: list[object] = []
    images: list[object] = []
    for block in data["mcp"]["content"]:
        if block.get("type") != "image":
            blocks.append(block)
            continue
        try:
            reference = store.save_image(
                b64decode(block["data"], validate=True),
                block["mimeType"],
            )
        except (AttachmentRejected, Base64Error) as exc:
            # 入口拒绝是可见事实，不静默丢图。
            blocks.append(
                {
                    "type": "image_rejected",
                    "mimeType": block["mimeType"],
                    "error": str(exc),
                }
            )
            continue
        blocks.append(reference.to_block())
        images.append(reference.to_block())
    if blocks == data["mcp"]["content"]:
        return result
    result = {
        **result,
        "data": {**data, "mcp": {**data["mcp"], "content": blocks}},
    }
    if images:
        result["images"] = images
    return result


def _loaded_from_spec(
    spec,
    attachments: AttachmentStore,
    *,
    provider_data: Mapping[str, object],
) -> LoadedTool:
    async def execute(arguments: Mapping[str, object]) -> object:
        return extract_images(
            runtime_tool_result(await spec.handler(arguments)),
            attachments,
        )

    return LoadedTool(
        name=spec.name,
        description=spec.description,
        parameters=dict(spec.parameters.schema()),
        execute=execute,
        requires_authorization=spec.requires_authorization,
        provider_data=provider_data,
    )
