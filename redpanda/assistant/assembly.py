from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import sys

from redpanda.assistant.artifacts import FileArtifactGateway
from redpanda.assistant.attachments import (
    AttachmentGateway,
    READ_IMAGE_SCHEMA,
    read_image_binding,
)
from redpanda.assistant.compact.core import CompactContext, CompactBoundary, FIND_HISTORY, SUBMIT
from redpanda.assistant.content import READ_CONTENT, READ_CONTENT_SCHEMA
from redpanda.assistant.loop_guard import LoopGuard
from redpanda.assistant.work_plan import UPDATE_PLAN, UPDATE_PLAN_SCHEMA, update_plan_binding
from redpanda.assistant.agents import project_agent
from redpanda.assistant.agent_messages import MESSAGE_SCHEMAS, message_bindings
from redpanda.automation.routine_tool import ROUTINE_SCHEMAS, ROUTINE_TOOLS, routine_bindings
from redpanda.assistant.delivery import (
    DELIVER_TOOL_NAME,
    PreviewEmitter,
)
from redpanda.assistant.context.prompt import BOT_PROMPT, DEFAULT_ASSISTANT_PROMPT, environment_prompt
from redpanda.assistant.host.command_port import WorkerCommandPort
from redpanda.assistant.context.projection import (
    ModelContextProjector,
    ModelContextSettings,
)
from redpanda.assistant.control import AssistantControlPlane
from redpanda.assistant.decision import (
    JournalBackedLlmDecisionMaker,
    bind_executor_tools,
    with_command_interrupts,
)
from redpanda.assistant.runner import SessionScheduler
from redpanda.assistant.agent_tools import AGENT_GIT_SCHEMAS, AGENT_GIT_TOOLS, EXCLUSIVE_PROJECT_TOOLS, agent_git_bindings
from redpanda.assistant.sessions import AssistantSessions
from redpanda.assistant.subagent.subagent import SubAgentHost, project_task
from redpanda.assistant.subagent.review import GitChildReview
from redpanda.assistant.toolsets import (
    LOAD_TOOLSET,
    LOAD_TOOLSET_DESCRIPTION,
    ToolSurface,
    load_toolset_binding,
)
from redpanda.runtime import AgentRuntime, ToolBinding
from redpanda.automation.tool import (
    CANCEL_SCHEDULE,
    CANCEL_SCHEDULE_SCHEMA,
    SCHEDULE_ONCE,
    SCHEDULE_ONCE_SCHEMA,
    cancel_schedule_binding,
    schedule_once_binding,
)
from redpanda.assistant.builtin_tools import build_builtin_tools, subagent_review_tools
from redpanda.filesystem.registry import WorkspaceRecord
from redpanda.filesystem.workspace import FileWorkspace
from redpanda.assistant.cli import CliToolAdapter
from redpanda.assistant.mcp import McpToolsetAdapter
from redpanda.assistant.management import (
    ManagementDomain,
    ManagementSurface,
    ResidentTool,
)
from redpanda.assistant.skills import SkillToolAdapter
from redpanda.assistant.catalog import CapabilityCatalog
from redpanda.assistant.shared_memory import (
    SHARED_MEMORY_SCHEMAS, SharedMemoryStore, shared_memory_bindings, sync_shared_memory,
)
from redpanda.config import AssistantConfig
from redpanda.paths import RedPandaHome, runtime_data_root
from redpanda.cli.composition import CliAssembly, build_cli
from redpanda.mcp.composition import McpAssembly, build_mcp
from redpanda.skills.composition import SkillAssembly, build_skills
from redpanda.skills.summarizer import LlmSkillDiffSummarizer


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
    read_attachment: Callable[[str], bytes]
    compact: CompactBoundary | None = None


async def build_assistant_assembly(
    config: AssistantConfig,
    sink,
    journal,
    *,
    session_id: str,
    workspace: WorkspaceRecord | FileWorkspace,
    command_environment: Mapping[str, str] | None = None,
    context_usage_sink: Callable[[str, int, int], None] | None = None,
    subagent_activity_sink: Callable[[str, bool], None] | None = None,
    tool_progress_sink=None,
    authorization_required_sink=None,
    preview_sink=None,
    thinking_sink=None,
    subagent_output_sink=None,
    session_failed_sink: Callable[[str, str], Awaitable[None] | None] | None = None,
    scheduler_factory=SessionScheduler,
    session_transport=None,
    model_selection_source=None,
    home: RedPandaHome | None = None,
    agent_branch: bool = False,
) -> AssistantAssembly:
    sessions_root = runtime_data_root() if home is None else home.runtime_sessions_root
    home = RedPandaHome.default() if home is None else home
    task = project_task(await journal.snapshot(session_id))
    agent = project_agent(await journal.snapshot(session_id))
    colleague_schemas = MESSAGE_SCHEMAS if agent is not None and session_transport is not None else []
    colleague_bindings = (message_bindings(session_transport, journal) if colleague_schemas else {})
    attachment_gateway = AttachmentGateway(sessions_root)
    attachments = attachment_gateway.for_session(session_id)
    builtin_tools = await build_builtin_tools(
        workspace, materials_root=attachments.files.materials, isolated=task is not None,
        personal_root=None if agent is None else agent.directory,
        colleague_roots=(home.agent_directories_root, home.agent_worktrees_root) if colleague_schemas else (),
        command_environment=command_environment,
        commands=(WorkerCommandPort(session_transport, session_id)
                   if sys.platform in {"win32", "linux"} and task is None and session_transport is not None else None),
    )
    command_interrupts = builtin_tools.command_interrupts

    async def review_operation(*args, **kwargs):
        return await child_review.review(*args, **kwargs)

    # A Bot links projects; its tasks delegated into one are branches merged back into its own branch.
    agent_git = agent is not None and session_transport is not None
    review_schemas, review_bindings, exclusive_tools = (
        subagent_review_tools(review_operation) if agent_git else ([], {}, frozenset())
    )
    if agent_git:
        exclusive_tools |= EXCLUSIVE_PROJECT_TOOLS
    settings = ModelContextSettings()
    gateway = FileArtifactGateway(sessions_root)
    projector = ModelContextProjector(
        gateway=gateway,
        attachments=attachment_gateway,
        settings=settings,
    )
    home.initialize()
    shared_memory = SharedMemoryStore(home.state_root / "shared_memory.sqlite") if agent is not None else None
    mcp = build_mcp(home)
    skills = build_skills(
        home,
        diff_summarizer=LlmSkillDiffSummarizer(
            config.llm,
            lambda: decision.model,
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
                "Skill 的查询、检查、安装、更新、启停与卸载",
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
    skill_tools = SkillToolAdapter(skills, gateway, settings,
                                  project_root=workspace.task_root)
    cli_tools = CliToolAdapter(cli, gateway, settings)
    subagents = SubAgentHost(subagent_activity_sink, agent_branch=agent_branch, projects=agent is not None)
    if task is not None:
        subagents._parents[session_id] = task.parent_session_id
    preview = PreviewEmitter(preview_sink, thinking_sink)

    async def report_session_failed(session_id: str, text: str) -> None:
        if subagents.is_subagent(session_id) or session_failed_sink is None:
            return
        observed = session_failed_sink(session_id, text)
        if isinstance(observed, Awaitable):
            await observed

    surface = ToolSurface(
        providers=(McpToolsetAdapter(mcp, attachments, read_only_only=task is not None),),
        base_schemas=[
            *builtin_tools.schemas,
            *review_schemas,
            *colleague_schemas,
            *(SHARED_MEMORY_SCHEMAS if shared_memory is not None else []),
            *(ROUTINE_SCHEMAS if colleague_schemas else []),
            *(AGENT_GIT_SCHEMAS if agent_git else []),
            *(
                [SCHEDULE_ONCE_SCHEMA, CANCEL_SCHEDULE_SCHEMA]
                if session_transport is not None else []
            ),
            READ_CONTENT_SCHEMA,
            READ_IMAGE_SCHEMA,
            UPDATE_PLAN_SCHEMA,
        ],
        reserved_names=(
            *builtin_tools.names(),
            *review_bindings,
            *colleague_bindings,
            *(schema["function"]["name"] for schema in SHARED_MEMORY_SCHEMAS),
            *ROUTINE_TOOLS,
            *AGENT_GIT_TOOLS,
            SCHEDULE_ONCE,
            CANCEL_SCHEDULE,
            READ_CONTENT,
            "read_image",
            UPDATE_PLAN,
            DELIVER_TOOL_NAME,
            *subagents.bindings(),
            FIND_HISTORY,
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
        restricted=task is not None,
    )
    compact_context = CompactContext(
        session_id,
        await journal.snapshot(session_id),
        projector,
        session_transport,
    )
    bindings = {
        **bind_executor_tools(
            builtin_tools,
            gateway,
            settings,
        ),
        **review_bindings,
        **colleague_bindings,
        **(shared_memory_bindings(shared_memory, agent.name) if shared_memory is not None else {}),
        **(routine_bindings(session_transport) if colleague_schemas else {}),
        **(agent_git_bindings(session_transport) if agent_git else {}),
        **(
            {
                SCHEDULE_ONCE: schedule_once_binding(session_transport),
                CANCEL_SCHEDULE: cancel_schedule_binding(session_transport),
            } if session_transport is not None else {}
        ),
        **read_image_binding(journal, attachments),
        UPDATE_PLAN: update_plan_binding(),
        **subagents.delivery_binding(sink, subagent_output_sink, preview),
        **load_toolset_binding(surface),
        **skill_tools.bindings(),
        **cli_tools.bindings(),
        **management.bindings(),
        **subagents.bindings(),
        **compact_context.bindings(),
    }
    if "execute_command" in bindings:
        bindings["execute_command"] = with_command_interrupts(bindings["execute_command"], command_interrupts)
    bindings = _with_tool_progress(bindings, tool_progress_sink)
    decision = JournalBackedLlmDecisionMaker(
        journal,
        config.llm,
        config.model_name,
        system_prompt=BOT_PROMPT if agent is not None else DEFAULT_ASSISTANT_PROMPT,
        environment=environment_prompt(
            workspace.task_root, full_access=workspace.full_access,
            own_copy=agent_branch, delegated=task is not None,
            personal_directory=None if agent is None else agent.directory,
        ),
        surface=surface,
        skill_tools=skill_tools,
        cli_tools=cli_tools,
        projector=projector,
        control=control,
        management=management,
        compact_threshold_tokens=config.compact_threshold_tokens,
        reasoning_effort=config.reasoning_effort,
        context_usage_sink=context_usage_sink,
        subagents=subagents,
        compact=compact_context,
        exclusive_tool_names=exclusive_tools,
        loop_guard=LoopGuard(),
        preview=preview,
    )
    runtime = AgentRuntime(journal, decision, bindings)
    child_review = GitChildReview(runtime, session_id, session_transport)
    surface.attach(runtime)
    compact_context.runtime = runtime

    scheduler = scheduler_factory(
        runtime,
        session_id,
        control=control,
        # 子 Session 失败提示不外露：用户该看到的是父转述后的判断。
        on_failed=subagents.on_failed,
        session_failed=report_session_failed,
        preview=preview,
    )
    scheduler.command_interrupts = command_interrupts
    compact = None
    if session_transport is not None:
        compact = CompactBoundary(
            runtime, decision, compact_context, config, control, session_transport,
            model_selection_source=model_selection_source,
        )
        compact.scheduler = scheduler

        scheduler.propagate_failures = compact_context.is_reader
    subagents.attach(runtime, session_transport)
    sessions = AssistantSessions(
        runtime,
        surface,
        scheduler,
        control=control,
        management=management,
        catalog=catalog,
        subagents=subagents,
    )

    async def before_advance():
        if not compact_context.is_reader:
            if not (await runtime.state(session_id)).waiting_command_ids:
                await catalog.sync(runtime, session_id)
                if shared_memory is not None:
                    await sync_shared_memory(runtime, session_id, shared_memory)
        return True if compact is None else await compact.before_advance()

    scheduler.before_advance = before_advance

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
        read_attachment=(
            compact_context.read_attachment
            if compact_context.is_reader
            else attachments.read
        ),
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
