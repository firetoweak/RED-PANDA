import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mcp.types import CallToolRequest, CallToolRequestParams

from redpanda.assistant.attachments import AttachmentStore
from redpanda.assistant.mcp import _loaded_from_spec
from redpanda.runtime.model import InvokeTool
from redpanda.tools.spec import JsonSchemaParameters, ToolSpec


class McpArgumentsTest(unittest.IsolatedAsyncioTestCase):
    async def test_frozen_nested_arguments_serialize_as_mcp_request(self):
        arguments = {
            "options": {"viewport": {"width": 1280, "height": 720}},
            "items": [{"name": "main", "flags": [True, None, 1.5]}],
        }
        requests = []

        async def handler(payload):
            request = CallToolRequest(
                method="tools/call",
                params=CallToolRequestParams(name="browser", arguments=payload),
            )
            requests.append(request.model_dump(mode="json", exclude_none=True))
            return {"ok": True, "code": "OK"}

        with TemporaryDirectory() as directory:
            tool = _loaded_from_spec(
                ToolSpec(
                    name="browser",
                    description="Browser tool",
                    parameters=JsonSchemaParameters({"type": "object"}),
                    handler=handler,
                ),
                AttachmentStore(Path(directory)),
                provider_data={},
            )
        effect = InvokeTool("browser", tuple(arguments.items()))
        self.assertEqual(
            await tool.execute(effect.argument_dict()),
            {"ok": True, "code": "OK"},
        )
        self.assertEqual(requests[0]["params"]["arguments"], arguments)
        self.assertIsInstance(dict(effect.arguments)["items"], tuple)
