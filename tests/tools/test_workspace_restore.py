import asyncio
from unittest.mock import AsyncMock

import pytest

from redpanda.assistant.decision import decision_from_llm
from redpanda.assistant.subagent.subagent import CHILD_BUILTIN_TOOL_NAMES
from redpanda.llm.api import InvalidLLMResponse, LLMResponse, ToolCall
from redpanda.tools.builtin.workspace_restore import create_workspace_restore_spec
from redpanda.tools.spec import ToolArgumentsError


def test_restore_declaration_is_exclusive_unapproved_and_uses_call_identity():
    operation = AsyncMock(return_value={"ok": True, "code": "WORKSPACE_RESTORED"})
    spec = create_workspace_restore_spec(operation)
    assert spec.requires_authorization is False
    assert spec.exclusive_batch is True
    assert spec.name in CHILD_BUILTIN_TOOL_NAMES
    assert set(spec.parameters.schema()["properties"]) == {"tool_call_id", "policy"}
    with pytest.raises(ToolArgumentsError):
        spec.parameters.validate({"version": "a" * 40})
    asyncio.run(spec.handler(spec.parameters.validate({"tool_call_id": "call-1"})))
    operation.assert_awaited_once_with("call-1", "preserve")


def test_exclusive_batch_uses_declarations_not_tool_names():
    calls = (ToolCall("one", "arbitrary_exclusive", "{}"), ToolCall("two", "read", "{}"))
    with pytest.raises(InvalidLLMResponse, match="only tool call"):
        decision_from_llm(LLMResponse(calls=calls), {"arbitrary_exclusive", "read"}, {"arbitrary_exclusive"})
