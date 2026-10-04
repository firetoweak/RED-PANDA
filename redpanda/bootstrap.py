from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from thinllm import ChatCompletionsClient

from redpanda.assistant.host.process_env import install_host_environment
from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.assistant.conversations import AssistantQueries
from redpanda.assistant.delivery import DeliverySink, PreviewSink
from redpanda.config import AppConfig, assistant_config_from_app, load_app_config, config_path, write_json
from redpanda.model_settings import ModelSettings
from redpanda.llm.connections import ModelConnections
from redpanda.paths import RedPandaHome
from redpanda.mcp.composition import build_mcp
from redpanda.sandbox.registry import WorkspaceRecord, WorkspaceRegistry
from redpanda.skills.composition import build_skills
from redpanda.skills.summarizer import LlmSkillDiffSummarizer


class UnboundHostLlm:
    async def chat(self, *args, **kwargs):
        raise RuntimeError("Worker must bind the Host LLM port")


def worker_config(app_config: AppConfig):
    """Worker 只拿配置；它这条会话的工作区由会话自己的日志决定。"""
    return assistant_config_from_app(app_config, UnboundHostLlm())


@dataclass(frozen=True, slots=True)
class BootstrappedAssistant:
    config: AppConfig
    sessions: HostSupervisor
    sessions_root: Path
    mcp_service: object
    skill_service: object
    queries: AssistantQueries
    workspaces: WorkspaceRegistry
    workspace: WorkspaceRecord | None


@asynccontextmanager
async def bootstrap_assistant(
    sink: DeliverySink,
    *,
    app_config: AppConfig | None = None,
    workspace_path: Path | None = None,
    context_usage_sink=None,
    subagent_activity_sink=None,
    conversation_status_sink=None,
    tool_progress_sink=None,
    authorization_required_sink=None,
    preview_sink: PreviewSink | None = None,
    thinking_sink=None,
    subagent_output_sink=None,
    session_activity_sink=None,
    session_failed_sink=None,
    schedule_changed_sink=None,
) -> AsyncIterator[BootstrappedAssistant]:
    install_host_environment()
    config = load_app_config() if app_config is None else app_config
    home = RedPandaHome.default()
    home.initialize()
    store = SessionStore(home.runtime_sessions_root)
    if app_config is not None and not config_path().exists():
        write_json(config_path(), app_config.to_dict())
    models = ModelSettings(home, store.root)
    if app_config is not None:
        models.save(app_config.to_dict())
    llm = ModelConnections(home.connections_path, client_factory=ChatCompletionsClient)
    workspaces = WorkspaceRegistry.load(home.workspaces_path)
    # 只有调用方明确给出路径才登记。TUI / ACP 传入启动目录或 --workspace；
    # Web 缺省不从进程 cwd 偷建工作区。
    workspace = (
        None if workspace_path is None else workspaces.register_path(workspace_path)
    )
    host = HostSupervisor(
        store,
        partial(worker_config, config),
        home,
        sink,
        workspaces=workspaces,
        llm=llm,
        models=models,
        context_usage_sink=context_usage_sink,
        subagent_activity_sink=subagent_activity_sink,
        conversation_status_sink=conversation_status_sink,
        tool_progress_sink=tool_progress_sink,
        authorization_required_sink=authorization_required_sink,
        preview_sink=preview_sink,
        thinking_sink=thinking_sink,
        subagent_output_sink=subagent_output_sink,
        session_activity_sink=session_activity_sink,
        session_failed_sink=session_failed_sink,
        schedule_changed_sink=schedule_changed_sink,
    )
    mcp = build_mcp(home)
    skills = build_skills(
        home, diff_summarizer=LlmSkillDiffSummarizer(llm, lambda: models.config().default_model or "")
    )
    async with llm, mcp.client_manager, asyncio.TaskGroup() as tasks:
        host.start_automation(tasks)
        try:
            yield BootstrappedAssistant(
                config,
                host,
                store.root,
                mcp.service,
                skills.service,
                AssistantQueries(store, host),
                workspaces,
                workspace,
            )
        finally:
            await host.close()
