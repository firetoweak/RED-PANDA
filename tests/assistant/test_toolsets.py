from __future__ import annotations

import unittest
from collections.abc import Mapping

from redpanda.assistant.delivery import DELIVER_TOOL_NAME, deliver_binding
from tests.session_scheduler import settle_session
from redpanda.assistant.toolsets import (
    LOAD_TOOLSET,
    LoadedTool,
    LoadedToolSnapshot,
    ToolSurface,
    ToolsetDescriptor,
    ToolsetLoadError,
    load_toolset_binding,
)
from redpanda.runtime import (
    AgentRuntime,
    CommandOutcomeReceived,
    InvokeTool,
    MemoryJournal,
    ModelDecision,
    RuntimeStatus,
)
from tests.assistant.test_runner import ScriptedDecisionMaker, SequentialIds


SESSION_ID = "toolset-session"


async def _ping(arguments: Mapping[str, object]) -> object:
    return {"ok": True, "code": "ECHO", "data": {"echo": arguments.get("text", "")}}


class FakeEchoProvider:
    def __init__(self, revision: int = 1, *, requires_authorization: bool = False) -> None:
        self.revision = revision
        self.requires_authorization = requires_authorization
        self.load_calls = 0
        self.restore_calls = 0

    def descriptors(self) -> tuple[ToolsetDescriptor, ...]:
        return (ToolsetDescriptor("demo", "echo tools", self.revision),)

    def handles(self, toolset_id: str) -> bool:
        return toolset_id == "demo"

    async def load(self, toolset_id: str, revision: int) -> tuple[LoadedTool, ...]:
        self.load_calls += 1
        if toolset_id != "demo" or revision != self.revision:
            raise ToolsetLoadError(
                "TOOLSET_REVISION_UNAVAILABLE",
                f"Toolset {toolset_id} revision is unavailable",
                data={"expected_revision": revision, "available_revision": self.revision},
            )
        return (
            LoadedTool(
                name="demo_ping",
                description="echo text",
                parameters={"type": "object", "properties": {"text": {"type": "string"}}},
                execute=_ping,
                requires_authorization=self.requires_authorization,
            ),
        )

    def restore(
        self,
        toolset_id: str,
        revision: int,
        tools: tuple[LoadedToolSnapshot, ...],
    ) -> tuple[LoadedTool, ...]:
        self.restore_calls += 1
        if toolset_id != "demo":
            raise ToolsetLoadError("TOOLSET_NOT_FOUND", f"Toolset {toolset_id} not found")
        return tuple(
            LoadedTool(
                name=tool.name,
                description=tool.description,
                parameters=dict(tool.parameters),
                execute=_ping,
                requires_authorization=tool.requires_authorization,
                provider_data=tool.provider_data,
            )
            for tool in tools
        )


class NestedSchemaProvider(FakeEchoProvider):
    """形状同真实 MCP Toolset：参数 Schema 与 provider_data 都含嵌套对象。"""

    def __init__(self) -> None:
        super().__init__()
        self.snapshots: tuple[LoadedToolSnapshot, ...] = ()

    async def load(self, toolset_id: str, revision: int) -> tuple[LoadedTool, ...]:
        if toolset_id != "demo":
            raise ToolsetLoadError("TOOLSET_NOT_FOUND", f"Toolset {toolset_id} not found")
        return (
            LoadedTool(
                name="demo_ping",
                description="echo text",
                parameters={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                execute=_ping,
                provider_data={
                    "tool_name": "demo_ping",
                    "output_schema": {
                        "type": "object",
                        "properties": {"echo": {"type": "string"}},
                    },
                },
            ),
        )

    def restore(self, toolset_id, revision, tools):
        self.snapshots = tools
        return super().restore(toolset_id, revision, tools)


def _schema_names(schemas: list[dict[str, object]]) -> set[str]:
    names: set[str] = set()
    for schema in schemas:
        function = schema.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            names.add(function["name"])
    return names


def _deliver(text: str = "done") -> ModelDecision:
    return ModelDecision(
        content=text,
        command_requests=(InvokeTool(DELIVER_TOOL_NAME, (("output_id", "output-1"), ("text", text))),),
    )


def _load(toolset_id: str = "demo") -> ModelDecision:
    return ModelDecision(
        content="loading",
        command_requests=(InvokeTool(LOAD_TOOLSET, (("toolset_id", toolset_id),)),),
    )


def _attached(provider=None, scripts=(), delivered=None, **surface_options):
    """装好一个已应用当前目录的 ToolSurface 和挂上它的 runtime。"""

    surface = ToolSurface(
        providers=(FakeEchoProvider() if provider is None else provider,),
        **surface_options,
    )
    surface.apply_catalog(SESSION_ID, surface.registry_descriptors())
    bindings = dict(load_toolset_binding(surface))
    if delivered is not None:
        bindings.update(deliver_binding(lambda _s, _o, text: delivered.append(text)))
    runtime = AgentRuntime(MemoryJournal(), ScriptedDecisionMaker(scripts), bindings, SequentialIds())
    surface.attach(runtime)
    return surface, runtime


class ToolsetProgressiveLoadTest(unittest.IsolatedAsyncioTestCase):
    async def _committed_load_events(self, provider=None):
        _surface, runtime = _attached(
            provider, (lambda _f: _load(), lambda _f: _deliver()), delivered=[],
        )
        await runtime.receive_user_message(SESSION_ID, "load echo", delivery_id="load-1")
        await settle_session(runtime, SESSION_ID)
        return await runtime.snapshot(SESSION_ID)

    def test_catalog_does_not_expose_loaded_tools_before_load(self):
        surface = ToolSurface(providers=(FakeEchoProvider(),))
        surface.apply_catalog(SESSION_ID, surface.registry_descriptors())
        self.assertEqual(_schema_names(surface.schemas(SESSION_ID)), {LOAD_TOOLSET})
        self.assertIn("demo", surface.catalog_instruction(SESSION_ID))
        self.assertNotIn("demo_ping", surface.catalog_instruction(SESSION_ID))

    async def test_unknown_toolset_is_a_model_correctable_error(self):
        surface, _runtime = _attached()
        result = await surface.load(SESSION_ID, "missing")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "TOOLSET_NOT_FOUND")
        self.assertEqual(_schema_names(surface.schemas(SESSION_ID)), {LOAD_TOOLSET})

    async def test_load_rejects_registry_revision_newer_than_session_catalog(self):
        provider = FakeEchoProvider()
        surface = ToolSurface(providers=(provider,))
        surface.apply_catalog(SESSION_ID, surface.registry_descriptors())
        provider.revision = 2

        result = await surface.load(SESSION_ID, "demo")

        self.assertEqual(result["code"], "TOOLSET_REVISION_UNAVAILABLE")
        self.assertEqual(result["data"]["expected_revision"], 1)
        self.assertEqual(result["data"]["available_revision"], 2)

    async def test_catalog_removal_does_not_remove_loaded_snapshot(self):
        surface, _runtime = _attached()
        self.assertTrue((await surface.load(SESSION_ID, "demo"))["ok"])

        surface.apply_catalog(SESSION_ID, ())

        self.assertEqual(_schema_names(surface.schemas(SESSION_ID)), {"demo_ping"})

    async def test_load_toolset_makes_tools_visible_on_the_next_step(self):
        delivered: list[str] = []
        seen: list[set[str]] = []
        surface = None

        def first(_frame):
            seen.append(_schema_names(surface.schemas(SESSION_ID)))
            return _load()

        def second(_frame):
            seen.append(_schema_names(surface.schemas(SESSION_ID)))
            return ModelDecision(
                content="pinging",
                command_requests=(InvokeTool("demo_ping", (("text", "hi"),)),),
            )

        surface, runtime = _attached(
            scripts=(first, second, lambda _f: _deliver()),
            delivered=delivered,
            reserved_names=(DELIVER_TOOL_NAME,),
        )
        await runtime.receive_user_message(SESSION_ID, "use echo", delivery_id="ask-1")
        result = await settle_session(runtime, SESSION_ID)
        echoes = [
            event.payload.outcome.value
            for event in await runtime.snapshot(SESSION_ID)
            if isinstance(event.payload, CommandOutcomeReceived)
            and isinstance(event.payload.outcome.value, Mapping)
            and event.payload.outcome.value.get("code") == "ECHO"
        ]

        self.assertEqual(result.state.status, RuntimeStatus.WAITING)
        self.assertEqual(delivered, ["done"])
        self.assertEqual(seen, [{LOAD_TOOLSET}, {LOAD_TOOLSET, "demo_ping"}])
        self.assertEqual(echoes[0]["data"]["echo"], "hi")

    async def test_loaded_toolset_cache_can_be_rehydrated_from_journal(self):
        events = await self._committed_load_events()
        provider = FakeEchoProvider()
        surface, _runtime = _attached(provider)

        activations = await surface.rehydrate(SESSION_ID, events)

        self.assertEqual([(item.toolset_id, item.revision) for item in activations], [("demo", 1)])
        self.assertEqual(_schema_names(surface.schemas(SESSION_ID)), {LOAD_TOOLSET, "demo_ping"})
        self.assertEqual(provider.load_calls, 0)
        self.assertEqual(provider.restore_calls, 1)

    async def test_rehydrate_hands_plain_json_containers_to_provider(self):
        events = await self._committed_load_events(NestedSchemaProvider())
        provider = NestedSchemaProvider()
        surface, _runtime = _attached(provider)

        await surface.rehydrate(SESSION_ID, events)

        snapshot = provider.snapshots[0]
        self.assertIs(type(snapshot.parameters), dict)
        self.assertIs(type(snapshot.parameters["properties"]), dict)
        self.assertIs(type(snapshot.provider_data), dict)
        self.assertIs(type(snapshot.provider_data["output_schema"]), dict)

    async def test_failed_load_outcome_does_not_break_rehydrate(self):
        _surface, runtime = _attached(
            scripts=(lambda _f: _load("missing"), lambda _f: _deliver()), delivered=[],
        )
        await runtime.receive_user_message(SESSION_ID, "load missing", delivery_id="missing-1")
        await settle_session(runtime, SESSION_ID)

        restored, _restored_runtime = _attached()
        activations = await restored.rehydrate(SESSION_ID, await runtime.snapshot(SESSION_ID))

        self.assertEqual(activations, ())
        self.assertEqual(_schema_names(restored.schemas(SESSION_ID)), {LOAD_TOOLSET})

    async def test_rehydrate_keeps_loaded_snapshot_across_registry_revision_change(self):
        events = await self._committed_load_events()
        surface, _runtime = _attached(FakeEchoProvider(revision=2))

        activations = await surface.rehydrate(SESSION_ID, events)

        self.assertEqual(activations[0].revision, 1)
        self.assertEqual(_schema_names(surface.schemas(SESSION_ID)), {LOAD_TOOLSET, "demo_ping"})

    async def test_dynamic_tool_freezes_host_authorization_requirement(self):
        surface, runtime = _attached(
            FakeEchoProvider(requires_authorization=True),
            (lambda _f: ModelDecision(content="ping", command_requests=(InvokeTool("demo_ping"),)),),
        )
        self.assertTrue((await surface.load(SESSION_ID, "demo"))["ok"])
        await runtime.receive_user_message(SESSION_ID, "ping", delivery_id="auth-1")

        step = (await runtime.advance(SESSION_ID)).step
        state = await runtime.state(SESSION_ID)

        self.assertTrue(step.commands[0].requires_authorization)
        self.assertEqual(state.waiting_for, (f"authorization:{step.commands[0].command_id}",))
