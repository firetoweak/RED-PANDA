from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from hashlib import sha256

from helperme.assistant.artifacts import (
    READ_ARTIFACT_SCHEMA,
    FileArtifactGateway,
    read_artifact_binding,
)
from helperme.assistant.attachments import (
    AttachmentGateway,
    READ_IMAGE_SCHEMA,
    read_image_binding,
)
from helperme.assistant.compact.core import CompactContext, CompactBoundary, READ, SUBMIT
from helperme.assistant.loop_guard import LoopGuard
from helperme.assistant.loop_guard_strategies import ConsecutiveActions
from helperme.assistant.delivery import (
    DELIVER_TOOL_NAME,
    PreviewEmitter,
    deliver_binding,
)
from helperme.assistant.context.projection import (
    ModelContextProjector,
    ModelContextSettings,
)
from helperme.assistant.control import AssistantControlPlane
from helperme.assistant.decision import (
    JournalBackedLlmDecisionMaker,
    bind_executor_tools,
)
from helperme.assistant.runner import SessionScheduler
from helperme.assistant.workspace_versions import WorkspaceVersionBoundary
from helperme.sandbox.versions import WorkspaceVersions
from helperme.assistant.sessions import AssistantSessions
from helperme.assistant.subagent.subagent import DELEGATE, REPORT, SubAgentHost
from helperme.assistant.toolsets import (
    LOAD_TOOLSET,
    LOAD_TOOLSET_DESCRIPTION,
    ToolSurface,
    load_toolset_binding,
)
from helperme.runtime import AgentRuntime, ToolBinding
from helperme.automation.tool import (
    CANCEL_SCHEDULE,
    CANCEL_SCHEDULE_SCHEMA,
    SCHEDULE_ONCE,
    SCHEDULE_ONCE_SCHEMA,
    cancel_schedule_binding,
    schedule_once_binding,
)
from helperme.assistant.builtin_tools import build_builtin_tools, workspace_restore_tool
from helperme.sandbox.registry import WorkspaceRecord
from helperme.assistant.cli import CliToolAdapter
from helperme.assistant.mcp import McpToolsetAdapter
from helperme.assistant.management import (
    ManagementDomain,
    ManagementSurface,
    ResidentTool,
)
from helperme.assistant.skills import SkillToolAdapter
from helperme.assistant.catalog import CapabilityCatalog
from helperme.config import AssistantConfig
from helperme.paths import HelperMeHome, runtime_data_root
from helperme.cli.composition import CliAssembly, build_cli
from helperme.mcp.composition import McpAssembly, build_mcp
from helperme.skills.composition import SkillAssembly, build_skills
from helperme.skills.summarizer import LlmSkillDiffSummarizer


@dataclass(frozen=True, slots=True)
class AssistantAssembly:
    runtime: AgentRuntime
    scheduler: SessionScheduler
    sessions: AssistantSessions
    bindings: dict[str, ToolBinding]
    surface: ToolSurface
    mcp: McpAssembly
    skills: SkillAssembly
    cli: CliAssembly
    control: AssistantControlPlane
    subagents: SubAgentHost
    catalog: CapabilityCatalog
    compact: CompactBoundary | None = None


def _model_context_settings(config: AssistantConfig) -> ModelContextSettings:
    return ModelContextSettings(
        context_limit=config.model_context_limit,
        input_budget_ratio=config.input_budget_ratio,
    )


async def build_assistant_assembly(
    config: AssistantConfig,
    sink,
    journal,
    *,
    session_id: str,
    workspace: WorkspaceRecord,
    context_usage_sink: Callable[[str, int, int], None] | None = None,
    subagent_activity_sink: Callable[[str, bool], None] | None = None,
    tool_progress_sink=None,
    authorization_required_sink=None,
    preview_sink=None,
    thinking_sink=None,
    session_failed_sink: Callable[[str, str], Awaitable[None] | None] | None = None,
    scheduler_factory=SessionScheduler,
    session_transport=None,
    home: HelperMeHome | None = None,
) -> AssistantAssembly:
    sessions_root = runtime_data_root() if home is None else home.runtime_sessions_root
    home = HelperMeHome.default() if home is None else home
    versions = WorkspaceVersions(
        workspace.task_root,
        home.state_root / "workspace_versions" / sha256(
            workspace.workspace_id.encode("utf-8")
        ).hexdigest(),
        # Journal/产品资源属于 Host，即使数据目录位于任务根内也不能随文件回退。
        excluded_roots=(home.root,),
    )
    attachment_gateway = AttachmentGateway(sessions_root)
    attachments = attachment_gateway.for_session(session_id)
    builtin_tools = await build_builtin_tools(
        workspace, materials_root=attachments.files.materials,
    )
    command_interrupts = builtin_tools.command_interrupts

    async def restore_workspace(command_id, target):
        events = await runtime.snapshot(session_id)
        state = runtime.projector.project_visible(session_id, events)
        visible = compact_context.visible(events, state)
        return await version_boundary.restore(command_id, target, events, visible)

    restore_schema, restore_binding, exclusive_tools = workspace_restore_tool(restore_workspace)
    settings = _model_context_settings(config)
    gateway = FileArtifactGateway(sessions_root)
    projector = ModelContextProjector(
        gateway=gateway,
        attachments=attachment_gateway,
        settings=settings,
    )
    home.initialize()
    mcp = build_mcp(home)
    skills = build_skills(
        home,
        diff_summarizer=LlmSkillDiffSummarizer(
            config.llm,
            config.model_name,
        ),
    )
    cli = build_cli(home)
    operations = (
        *mcp.control_operations,
        *skills.control_operations,
        *cli.control_operations,
    )
    control = AssistantControlPlane(operations)
    management = ManagementSurface(
        (
            ManagementDomain(
                "mcp",
                "MCP Server 的发现、诊断、安装、更新与修复",
                mcp.management_specs,
                mcp.control_operations,
                (ResidentTool(LOAD_TOOLSET, LOAD_TOOLSET_DESCRIPTION),),
            ),
            ManagementDomain(
                "skill",
                "Skill 的帮助、查询、检查、安装、更新、启停与卸载",
                skills.management_specs,
                skills.control_operations,
                tuple(
                    ResidentTool(spec.name, spec.description)
                    for spec in skills.tool_catalog.tool_specs()
                ),
            ),
            ManagementDomain(
                "cli",
                "CLI 的登记、诊断、安装、更新与修复",
                cli.management_specs,
                cli.control_operations,
                tuple(
                    ResidentTool(spec.name, spec.description)
                    for spec in cli.tool_catalog.tool_specs()
                ),
            ),
        ),
        gateway,
        settings,
    )
    skill_tools = SkillToolAdapter(skills, gateway, settings)
    cli_tools = CliToolAdapter(cli, gateway, settings)
    subagents = SubAgentHost(subagent_activity_sink)
    preview = PreviewEmitter(preview_sink, thinking_sink)
    delivery_sink = subagents.routed_sink(sink)

    async def report_session_failed(session_id: str, text: str) -> None:
        if subagents.is_subagent(session_id) or session_failed_sink is None:
            return
        observed = session_failed_sink(session_id, text)
        if isinstance(observed, Awaitable):
            await observed

    surface = ToolSurface(
        providers=(McpToolsetAdapter(mcp, attachments),),
        base_schemas=[
            *builtin_tools.schemas,
            restore_schema,
            *(
                [SCHEDULE_ONCE_SCHEMA, CANCEL_SCHEDULE_SCHEMA]
                if session_transport is not None else []
            ),
            READ_ARTIFACT_SCHEMA,
            READ_IMAGE_SCHEMA,
        ],
        reserved_names=(
            *builtin_tools.names(),
            restore_schema["function"]["name"],
            SCHEDULE_ONCE,
            CANCEL_SCHEDULE,
            "read_artifact",
            "read_image",
            DELIVER_TOOL_NAME,
            DELEGATE,
            REPORT,
            READ,
            SUBMIT,
            *management.names(),
            *management.resident_names(),
            *(operation.name for operation in operations),
        ),
        gateway=gateway,
        settings=settings,
    )
    catalog = CapabilityCatalog(
        surface,
        skill_tools,
        cli_tools,
        management,
    )
    compact_context = CompactContext(
        session_id,
        await journal.snapshot(session_id),
        projector,
        session_transport,
    )
    bind_reader = getattr(config.llm, "bind_attachment_reader", None)
    if bind_reader is not None:
        bind_reader(
            compact_context.read_attachment
            if compact_context.is_reader
            else attachments.read
        )
    bindings = {
        **bind_executor_tools(
            builtin_tools,
            gateway,
            settings,
            command_interrupts,
        ),
        restore_schema["function"]["name"]: restore_binding,
        **(
            {
                SCHEDULE_ONCE: schedule_once_binding(session_transport),
                CANCEL_SCHEDULE: cancel_schedule_binding(session_transport),
            } if session_transport is not None else {}
        ),
        **read_artifact_binding(gateway),
        **read_image_binding(journal, attachments),
        **deliver_binding(delivery_sink, preview),
        **load_toolset_binding(surface),
        **skill_tools.bindings(),
        **cli_tools.bindings(),
        **management.bindings(),
        **subagents.bindings(),
        **compact_context.bindings(),
    }
    bindings = _with_tool_progress(bindings, tool_progress_sink)
    decision = JournalBackedLlmDecisionMaker(
        journal,
        config.llm,
        config.model_name,
        surface=surface,
        skill_tools=skill_tools,
        cli_tools=cli_tools,
        projector=projector,
        control=control,
        management=management,
        context_usage_sink=context_usage_sink,
        subagents=subagents,
        compact=compact_context,
        exclusive_tool_names=exclusive_tools,
        loop_guard=LoopGuard((ConsecutiveActions(config.loop_guard_repeat_threshold),)),
        preview=preview,
    )
    runtime = AgentRuntime(journal, decision, bindings)
    surface.attach(runtime)
    compact_context.runtime = runtime
    scheduler = scheduler_factory(
        runtime,
        session_id,
        control=control,
        # 子 Session 失败提示不外露：用户该看到的是父转述后的判断。
        on_quiesced=subagents.on_quiesced,
        on_failed=subagents.on_failed,
        session_failed=report_session_failed,
        preview=preview,
    )
    scheduler.command_interrupts = command_interrupts
    compact = None
    if session_transport is not None:
        compact = CompactBoundary(
            runtime, decision, compact_context, config, control, session_transport
        )
        compact.scheduler = scheduler

        scheduler.propagate_failures = compact_context.is_reader
    subagents.attach(runtime, session_transport)
    version_boundary = WorkspaceVersionBoundary(runtime, session_id, workspace.workspace_id, versions)
    sessions = AssistantSessions(
        runtime,
        surface,
        scheduler,
        control=control,
        management=management,
        catalog=catalog,
        subagents=subagents,
        workspace_versions=version_boundary,
    )

    async def before_advance():
        if session_transport is not None and await session_transport(
            "is_paused", session_id, {}
        ):
            return False
        if not compact_context.is_reader and not subagents.is_subagent(session_id):
            if not (await runtime.state(session_id)).waiting_command_ids:
                await catalog.sync(runtime, session_id)
        return True if compact is None else await compact.before_advance()

    scheduler.before_advance = before_advance

    async def record_workspace_versions():
        if not compact_context.is_reader and not subagents.is_subagent(session_id):
            await version_boundary.sync()

    scheduler.record_workspace_versions = record_workspace_versions
    scheduler.auto_authorize = sessions.is_auto_authorized
    scheduler.authorization_required = authorization_required_sink
    return AssistantAssembly(
        runtime=runtime,
        scheduler=scheduler,
        sessions=sessions,
        compact=compact,
        bindings=bindings,
        surface=surface,
        mcp=mcp,
        skills=skills,
        cli=cli,
        control=control,
        subagents=subagents,
        catalog=catalog,
    )


def _with_tool_progress(bindings, sink):
    if sink is None:
        return bindings
    projected = {}
    for name, binding in bindings.items():
        if name == DELIVER_TOOL_NAME:
            projected[name] = binding
            continue

        async def handler(context, arguments, _name=name, _handler=binding.handler):
            sink(context.session_id, "start", context.command_id, _name, arguments)
            try:
                result = await _handler(context, arguments)
            except BaseException:
                sink(context.session_id, "fail", context.command_id, _name, None)
                raise
            sink(context.session_id, "finish", context.command_id, _name, result)
            return result

        projected[name] = ToolBinding(
            handler,
            decision_on_outcome=binding.decision_on_outcome,
            requires_authorization=binding.requires_authorization,
        )
    return projected
