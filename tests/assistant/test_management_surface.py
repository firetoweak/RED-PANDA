from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import BaseModel, ConfigDict

from redpanda.assistant.artifacts import FileArtifactGateway
from redpanda.assistant.delivery import DELIVER_TOOL_NAME, deliver_binding
from redpanda.assistant.management import (
    LOAD_MANAGEMENT_TOOLS,
    ManagementDomain,
    ManagementSurface,
    ResidentTool,
)
from redpanda.assistant.context.projection import ModelContextSettings
from tests.session_scheduler import settle_session
from redpanda.runtime import AgentRuntime, InvokeTool, MemoryJournal, ModelDecision
from redpanda.runtime.state import DecisionFrame
from redpanda.tools.control import ControlApprovalExecution, ControlOperation
from redpanda.tools.spec import PydanticParameters, ToolSpec


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _diagnose(_input: EmptyInput) -> dict[str, object]:
    return {"ok": True, "code": "HEALTHY", "data": {}}


def _spec(name: str) -> ToolSpec:
    return ToolSpec(
        name,
        f"{name} description",
        PydanticParameters(EmptyInput),
        _diagnose,
    )


class _ApprovalHandler:
    def __init__(self, action: str) -> None:
        self.action = action

    async def execute(self, _payload) -> ControlApprovalExecution:
        return ControlApprovalExecution(True, "done")


def _operation(domain: str, name: str) -> ControlOperation:
    return ControlOperation(
        domain,
        ToolSpec(
            name,
            f"{name} description",
            PydanticParameters(EmptyInput),
            _diagnose,
            control_boundary=True,
            exclusive_batch=True,
        ),
        _ApprovalHandler(f"{domain}.{name}"),
    )


def _names(schemas: list[dict[str, object]]) -> set[str]:
    return {schema["function"]["name"] for schema in schemas}


class ScriptedDecisionMaker:
    def __init__(self, management: ManagementSurface) -> None:
        self.management = management
        self.seen: list[set[str]] = []
        self.control_seen: list[frozenset[str]] = []

    async def decide(self, frame: DecisionFrame) -> ModelDecision:
        session_id = frame.state.session_id
        self.seen.append(_names(self.management.schemas(session_id, frame.state)))
        self.control_seen.append(self.management.control_names(session_id, frame.state))
        if len(self.seen) == 1:
            return ModelDecision(
                content="load mcp twice",
                command_requests=(
                    InvokeTool(
                        LOAD_MANAGEMENT_TOOLS,
                        (("domain", "mcp"),),
                    ),
                    InvokeTool(
                        LOAD_MANAGEMENT_TOOLS,
                        (("domain", "mcp"),),
                    ),
                ),
            )
        return ModelDecision(
            content="done",
            command_requests=(InvokeTool(DELIVER_TOOL_NAME, (("output_id", "output-1"), ("text", "done"))),),
        )


class ManagementProgressiveLoadTest(unittest.IsolatedAsyncioTestCase):
    async def test_domain_tools_appear_next_step_and_rehydrate(self):
        with TemporaryDirectory() as directory:
            gateway = FileArtifactGateway(Path(directory))
            domains = (
                ManagementDomain(
                    "mcp",
                    "mcp management",
                    (_spec("diagnose_mcp"),),
                    (_operation("mcp", "propose_mcp_repair"),),
                    (ResidentTool("load_toolset", "load a toolset"),),
                ),
                ManagementDomain(
                    "skill",
                    "skill management",
                    (_spec("diagnose_skill"),),
                    (_operation("skill", "propose_skill_install"),),
                    (ResidentTool("load_skill", "load a skill"),),
                ),
            )
            management = ManagementSurface(
                domains,
                gateway,
                ModelContextSettings(),
            )
            decisions = ScriptedDecisionMaker(management)
            delivered: list[str] = []
            runtime = AgentRuntime(
                MemoryJournal(),
                decisions,
                {
                    **management.bindings(),
                    **deliver_binding(lambda _session_id, _output_id, text: delivered.append(text)),
                },
            )
            await runtime.receive_user_message(
                "management-session",
                "repair mcp",
                delivery_id="user-1",
            )

            await settle_session(runtime, "management-session")
            events = await runtime.snapshot("management-session")

            self.assertEqual(decisions.seen[0], {LOAD_MANAGEMENT_TOOLS})
            self.assertEqual(
                decisions.seen[1],
                {LOAD_MANAGEMENT_TOOLS, "diagnose_mcp"},
            )
            self.assertEqual(
                decisions.control_seen[1],
                frozenset({"propose_mcp_repair"}),
            )
            self.assertNotIn("diagnose_skill", decisions.seen[1])
            self.assertEqual(delivered, ["done"])

            restored = ManagementSurface(
                domains,
                gateway,
                ModelContextSettings(),
            )
            activations = await restored.rehydrate(
                "management-session",
                events,
            )

            self.assertEqual(len(activations), 2)
            self.assertEqual(
                _names(restored.schemas("management-session")),
                {LOAD_MANAGEMENT_TOOLS, "diagnose_mcp"},
            )
            self.assertEqual(
                restored.control_names("management-session"),
                frozenset({"propose_mcp_repair"}),
            )

    async def test_loaded_skill_map_describes_every_registered_operation(self):
        from redpanda.paths import RedPandaHome
        from redpanda.skills.composition import build_skills

        with TemporaryDirectory() as directory:
            assembly = build_skills(RedPandaHome(Path(directory) / "home"))
            resident = tuple(
                ResidentTool(spec.name, spec.description)
                for spec in assembly.tool_catalog.tool_specs()
            )
            domain = ManagementDomain(
                "skill", "Skill 管理", assembly.management_specs, assembly.control_operations, resident,
            )
            surface = ManagementSurface(
                (domain,), FileArtifactGateway(Path(directory) / "artifacts"), ModelContextSettings(),
            )
            self.assertEqual(_names(surface.schemas("session")), {LOAD_MANAGEMENT_TOOLS})
            loaded = await surface.load("session", "skill", activation_command_id="load-1")
            specs = {item.name: item for item in assembly.management_specs}
            specs.update({item.name: item.proposal_spec for item in assembly.control_operations})
            expected = {
                name: ("control" if spec.control_boundary else "diagnostic", spec.description)
                for name, spec in specs.items()
            }
            expected.update({item.name: ("resident", item.description) for item in resident})
            entries = loaded["data"]["tools"]
            self.assertEqual({item["name"] for item in entries}, set(expected))
            for entry in entries:
                self.assertEqual((entry["kind"], entry["description"]), expected[entry["name"]])
            # 声明归属不改变呈现：常驻工具不因为加载管理域而变成管理域 schema。
            self.assertTrue(
                set(domain.resident_names).isdisjoint(_names(surface.schemas("session")))
            )

    async def test_failed_load_outcome_does_not_create_activation(self):
        with TemporaryDirectory() as directory:
            gateway = FileArtifactGateway(Path(directory))
            domains = (
                ManagementDomain(
                    "mcp",
                    "mcp management",
                    (_spec("diagnose_mcp"),),
                    (_operation("mcp", "propose_mcp_repair"),),
                    (),
                ),
            )
            management = ManagementSurface(
                domains,
                gateway,
                ModelContextSettings(),
            )

            class MissingDomainDecisionMaker:
                def __init__(self) -> None:
                    self.calls = 0

                async def decide(self, _frame: DecisionFrame) -> ModelDecision:
                    self.calls += 1
                    if self.calls == 1:
                        return ModelDecision(
                            command_requests=(
                                InvokeTool(
                                    LOAD_MANAGEMENT_TOOLS,
                                    (("domain", "missing"),),
                                ),
                            ),
                        )
                    return ModelDecision(
                        command_requests=(
                            InvokeTool(
                                DELIVER_TOOL_NAME,
                                (("output_id", "output-1"), ("text", "done")),
                            ),
                        ),
                    )

            runtime = AgentRuntime(
                MemoryJournal(),
                MissingDomainDecisionMaker(),
                {
                    **management.bindings(),
                    **deliver_binding(lambda _session_id, _output_id, _text: None),
                },
            )
            await runtime.receive_user_message(
                "management-session",
                "load missing",
                delivery_id="missing-1",
            )
            await settle_session(runtime, "management-session")

            restored = ManagementSurface(
                domains,
                gateway,
                ModelContextSettings(),
            )
            self.assertEqual(
                await restored.rehydrate(
                    "management-session",
                    await runtime.snapshot("management-session"),
                ),
                (),
            )
            self.assertEqual(
                _names(restored.schemas("management-session")),
                {LOAD_MANAGEMENT_TOOLS},
            )
