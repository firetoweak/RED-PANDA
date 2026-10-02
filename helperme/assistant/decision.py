from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import replace
from typing import AbstractSet, Protocol

from helperme.assistant.compact.core import (
    CompactContext,
    MODEL_USAGE,
    READ_SCHEMA,
    SUBMIT,
)
from helperme.assistant.artifacts import ArtifactGateway
from helperme.assistant.loop_guard import LoopGuard, NOTICE
from helperme.assistant.work_plan import WORK_PLAN_CONTEXT
from helperme.assistant.control import (
    CONTROL_REQUEST_METADATA,
    AssistantControlPlane,
    ControlArgumentsError,
)
from helperme.assistant.delivery import (
    DELIVER_TOOL_NAME,
    PreviewEmitter,
    ensure_deliver,
)
from helperme.assistant.context.projection import (
    MESSAGE_EXTENSIONS,
    ModelContextProjector,
    ModelContextSettings,
    externalize_tool_result,
)
from helperme.assistant.context.prompt import DEFAULT_ASSISTANT_PROMPT
from helperme.assistant.toolsets import ToolSurface
from helperme.assistant.management import ManagementSurface
from helperme.assistant.subagent.subagent import SubAgentHost
from helperme.runtime import (
    InvokeTool,
    ModelDecision,
    RecordedDecision,
    ToolBinding,
)
from helperme.runtime.model import AuthorizationPolicy
from helperme.runtime.dispatcher import AttemptContext
from helperme.tools.builtin import CommandInterrupts, run_interruptible
from helperme.runtime.state import DecisionFrame
from helperme.assistant.cli import CliToolAdapter
from helperme.assistant.skills import SkillToolAdapter
from helperme.llm.api import (
    InvalidLLMResponse,
    LLMApi,
    LLMResponse,
    ToolCall,
)


class ToolRunner(Protocol):
    def names(self) -> Sequence[str]: ...

    async def execute(
        self,
        name: str,
        arguments: Mapping[str, object],
    ) -> object: ...

    def requires_authorization(self, name: str) -> bool | AuthorizationPolicy: ...


def decision_from_llm(
    response: LLMResponse,
    allowed_tool_names: AbstractSet[str],
    exclusive_tool_names: AbstractSet[str] = frozenset(),
) -> ModelDecision:
    return ModelDecision(
        content=response.content,
        command_requests=_invoke_requests(
            response.calls,
            allowed_tool_names,
            exclusive_tool_names,
        ),
    )


def _invoke_requests(
    calls: Sequence[ToolCall],
    allowed_tool_names: AbstractSet[str],
    exclusive_tool_names: AbstractSet[str] = frozenset(),
) -> tuple[InvokeTool, ...]:
    if len(calls) != 1 and any(call.name in exclusive_tool_names for call in calls):
        raise InvalidLLMResponse(
            "invalid_tool_batch", "an exclusive tool must be the only tool call",
        )
    requests: list[InvokeTool] = []
    for call in calls:
        if call.name == DELIVER_TOOL_NAME:
            raise InvalidLLMResponse(
                "invalid_tool_call",
                "deliver is a product command, not a model tool",
            )
        if call.name not in allowed_tool_names:
            raise InvalidLLMResponse(
                "unknown_tool",
                f"tool {call.name} was not offered in this decision context",
            )
        try:
            payload = json.loads(call.arguments)
        except json.JSONDecodeError as exc:
            raise InvalidLLMResponse(
                "invalid_tool_arguments",
                f"tool {call.name} arguments are not valid JSON",
            ) from exc
        if type(payload) is not dict:
            raise InvalidLLMResponse(
                "invalid_tool_arguments",
                f"tool {call.name} arguments must be a JSON object",
            )
        requests.append(InvokeTool(call.name, tuple(payload.items())))
    return tuple(requests)


def _schema_name(schema: Mapping[str, object]) -> str:
    if set(schema) != {"type", "function"} or schema["type"] != "function":
        raise ValueError("tool schema envelope is invalid")
    function = schema["function"]
    if not isinstance(function, Mapping):
        raise TypeError("tool schema function must be an object")
    name = function["name"]
    if type(name) is not str or not name:
        raise ValueError("tool schema name must be a non-empty str")
    return name


def _tool_names(
    schemas: Sequence[dict[str, object]],
) -> frozenset[str]:
    names = [_schema_name(schema) for schema in schemas]
    if len(names) != len(set(names)):
        raise ValueError("tool schemas contain duplicate names")
    return frozenset(names)


def bind_executor_tools(
    runner: ToolRunner,
    gateway: ArtifactGateway,
    settings: ModelContextSettings,
    command_interrupts: CommandInterrupts | None = None,
) -> dict[str, ToolBinding]:
    bindings: dict[str, ToolBinding] = {}
    for name in runner.names():
        bindings[name] = ToolBinding(
            _executor_handler(runner, name, gateway, settings, command_interrupts),
            requires_authorization=runner.requires_authorization(name),
        )
    return bindings


def _executor_handler(
    runner: ToolRunner,
    name: str,
    gateway: ArtifactGateway,
    settings: ModelContextSettings,
    command_interrupts: CommandInterrupts | None,
):
    async def handler(
        context: AttemptContext,
        arguments: Mapping[str, object],
    ) -> object:
        async def execute() -> object:
            result = await runner.execute(name, arguments)
            return externalize_tool_result(
                result,
                context.session_id,
                gateway,
                settings,
            )

        if command_interrupts is not None and name == "execute_command":
            return await run_interruptible(
                command_interrupts,
                session_id=context.session_id,
                command_id=context.command_id,
                attempt_id=context.attempt_id,
                execute=execute,
            )
        return await execute()

    return handler


class JournalBackedLlmDecisionMaker:
    def __init__(
        self,
        journal,
        llm: LLMApi,
        model: str,
        *,
        surface: ToolSurface,
        skill_tools: SkillToolAdapter,
        cli_tools: CliToolAdapter,
        control: AssistantControlPlane,
        management: ManagementSurface,
        environment: str,
        system_prompt: str = DEFAULT_ASSISTANT_PROMPT,
        projector: ModelContextProjector | None = None,
        compact_threshold_tokens: int,
        context_usage_sink: Callable[[str, int, int], None] | None = None,
        subagents: SubAgentHost | None = None,
        compact: CompactContext | None = None,
        loop_guard: LoopGuard | None = None,
        preview: PreviewEmitter | None = None,
        exclusive_tool_names: AbstractSet[str] = frozenset(),
    ) -> None:
        self._exclusive_tool_names = exclusive_tool_names
        self._journal = journal
        self._llm = llm
        self._model = model
        self._system_prompt = system_prompt
        self._environment = environment
        self._projector = ModelContextProjector() if projector is None else projector
        self._surface = surface
        self._skill_tools = skill_tools
        self._cli_tools = cli_tools
        self._control = control
        self._management = management
        self._context_usage_sink = context_usage_sink
        self._compact_threshold_tokens = compact_threshold_tokens
        self._subagents = subagents
        self._compact = compact
        self._loop_guard = LoopGuard() if loop_guard is None else loop_guard
        self._preview = PreviewEmitter() if preview is None else preview

    def set_model(self, model: str, compact_threshold_tokens: int) -> None:
        self._model = model
        self._compact_threshold_tokens = compact_threshold_tokens

    @property
    def model(self) -> str:
        return self._model

    def schemas_for(
        self, state, events
    ) -> tuple[list[dict[str, object]], frozenset[str]]:
        if self._compact is not None and self._compact.is_reader:
            return deepcopy(self._compact.schemas()), frozenset()
        schemas = self._surface.base_schemas()
        schemas = [*schemas, *self._skill_tools.schemas()]
        schemas = [*schemas, *self._cli_tools.schemas()]
        schemas = [
            *schemas,
            *self._management.schemas(state.session_id, state),
        ]
        allowed_control_names = self._management.control_names(
            state.session_id,
            state,
        )
        control_schemas = self._control.schemas(
            state.session_id,
            events,
            allowed_control_names,
        )
        offered_control_names = _tool_names(control_schemas)
        schemas = [*schemas, *control_schemas]
        if self._compact is not None:
            schemas = [*schemas, *self._compact.schemas()]
        if self._subagents is not None:
            session_id = state.session_id
            schemas = [*schemas, *self._subagents.schemas(session_id)]
            allowed = self._subagents.tool_names(session_id)
            if allowed is not None:
                schemas = [
                    schema for schema in schemas if _schema_name(schema) in allowed
                ]
                offered_control_names = offered_control_names & allowed
        schemas = [*schemas, *self._surface.toolset_schemas(state.session_id, state)]
        return deepcopy(sorted(schemas, key=_schema_name)), offered_control_names

    def _prompt_for(self, frame: DecisionFrame) -> str:
        return self.prompt_for(frame.state)

    def prompt_for(self, state) -> str:
        session_id = state.session_id
        if self._compact is not None and self._compact.is_reader:
            return self._compact.request["messages"][0]["content"]
        prompt = self._system_prompt
        if self._subagents is not None:
            override = self._subagents.system_prompt(session_id)
            if override is not None:
                # 子使用独立提示词；能力目录仍由自己的 Journal 提供。
                prompt = override
        return f"{prompt}\n\n{self._environment}"

    def _decision_from_response(
        self,
        frame: DecisionFrame,
        response: LLMResponse,
        allowed_tool_names: AbstractSet[str],
        control_names: AbstractSet[str],
    ) -> ModelDecision:
        control_calls = tuple(
            call for call in response.calls if call.name in control_names
        )
        if control_calls and len(response.calls) != 1:
            raise InvalidLLMResponse(
                "invalid_control_batch",
                "a host control tool must be the only tool call",
            )
        if control_calls:
            call = control_calls[0]
            try:
                arguments = json.loads(call.arguments)
            except json.JSONDecodeError as exc:
                raise InvalidLLMResponse(
                    "invalid_tool_arguments",
                    f"tool {call.name} arguments are not valid JSON",
                ) from exc
            if type(arguments) is not dict:
                raise InvalidLLMResponse(
                    "invalid_tool_arguments",
                    f"tool {call.name} arguments must be a JSON object",
                )
            try:
                self._control.stage(frame, call.name, arguments)
            except ControlArgumentsError as exc:
                raise InvalidLLMResponse(
                    "invalid_tool_arguments",
                    f"tool {call.name} arguments violate its schema: {exc.details}",
                ) from exc
            return ModelDecision(
                content=(
                    response.content or "已提交需要用户确认的操作，等待用户确认。"
                ),
            )

        return ModelDecision(
            content=response.content,
            command_requests=_invoke_requests(
                response.calls,
                allowed_tool_names,
                self._exclusive_tool_names,
            ),
        )

    def with_loop_guard(self, prepared, events, position):
        notice = self._loop_guard.inspect(events, position)
        if notice is not None:
            messages = [*prepared.messages, {"role": "user", "content": notice["text"]}]
            prepared = replace(
                prepared, messages=messages,
                source_sequences=(*prepared.source_sequences, 0),
            )
        return prepared, notice

    async def decide(self, frame: DecisionFrame) -> RecordedDecision:
        # Journal facts are bounded by the frame position, freezing this Step's
        # visible world. Schemas read the same bounded events: a control proposal
        # still awaiting the user must not offer another control tool.
        self._control.begin_decision(frame.state.session_id)
        journal_tail = await self._journal.snapshot(frame.state.session_id)
        events = tuple(
            event
            for event in journal_tail
            if event.sequence <= frame.observed_journal_position
        )
        prompt = self._prompt_for(frame)
        schemas, control_names = self.schemas_for(frame.state, events)
        allowed_tool_names = _tool_names(schemas)
        visible = frame.state
        if self._compact is not None and self._compact.is_reader:
            prepared = await self._compact.prepare_reader(events, visible)
        else:
            if self._compact is not None:
                visible = self._compact.visible(events, visible)
            prepared = self._projector.prepare(
                events,
                visible,
                frame.state.session_id,
                prompt,
                prefix=None if self._compact is None else self._compact.prefix,
            )
        prepared, notice = self.with_loop_guard(
            prepared, events, frame.observed_journal_position
        )
        window = (
            None if self._compact is None or self._compact.window is None
            else self._compact.window["id"]
        )
        model = (
            self._compact.request["model"]
            if self._compact is not None and self._compact.is_reader
            else self._model
        )
        output_id = frame.trigger_event.event_id
        show_preview = (
            self._preview.enabled
            and not (self._compact is not None and self._compact.is_reader)
            and not (
                self._subagents is not None
                and self._subagents.is_subagent(frame.state.session_id)
            )
        )
        if show_preview:
            await self._preview.start(frame.state.session_id, output_id)

        async def on_content_delta(text: str) -> None:
            await self._preview.append(frame.state.session_id, output_id, text)

        async def on_reasoning_delta(text: str) -> None:
            await self._preview.start_thinking(frame.state.session_id, output_id)
            await self._preview.append_thinking(
                frame.state.session_id, output_id, text
            )

        try:
            if show_preview:
                result = await self._llm.chat(
                    prepared.messages,
                    model,
                    tools=schemas or None,
                    on_content_delta=on_content_delta,
                    on_reasoning_delta=on_reasoning_delta,
                )
            else:
                result = await self._llm.chat(
                    prepared.messages,
                    model,
                    tools=schemas or None,
                )
        except BaseException as error:
            try:
                await self._preview.abort(frame.state.session_id)
                await self._preview.abort_thinking(frame.state.session_id)
            except BaseException as preview_error:
                raise BaseExceptionGroup(
                    "model call and preview cleanup failed",
                    [error, preview_error],
                ) from None
            raise
        await self._preview.finish_thinking(frame.state.session_id, output_id)
        usage = result.usage
        if self._context_usage_sink is not None:
            self._context_usage_sink(
                frame.state.session_id,
                usage.input_tokens,
                self._compact_threshold_tokens,
            )
        if self._compact is not None and self._compact.is_reader:
            calls = result.response.calls
            if calls:
                decision = self._decision_from_response(
                    frame,
                    result.response,
                    {READ_SCHEMA["function"]["name"]} & allowed_tool_names,
                    frozenset(),
                )
            else:
                if not result.response.content.strip():
                    raise InvalidLLMResponse(
                        "invalid_handoff", "handoff must be nonempty"
                    )
                decision = ModelDecision(
                    content=result.response.content,
                    command_requests=(
                        InvokeTool(SUBMIT, (("handoff", result.response.content),)),
                    ),
                )
        else:
            decision = ensure_deliver(
                self._decision_from_response(
                    frame,
                    result.response,
                    allowed_tool_names,
                    control_names,
                ),
                output_id,
            )
            if show_preview and not decision.content.strip():
                await self._preview.abort(frame.state.session_id)
        manifest = {
            "schema": "decision-replay-manifest/v1",
            "decision_basis": {
                "trigger_event_id": frame.trigger_event.event_id,
                "decision_cursor": frame.decision_cursor,
                "basis_state_version": frame.basis_state_version,
                "observed_journal_position": frame.observed_journal_position,
                "visible_event_ids": list(frame.state.visible_event_ids),
            },
            "request": {
                "projector": "model-context/v1",
                "model": model,
                "messages": prepared.messages,
                "tools": schemas or None,
            },
            "response": {
                "content": result.response.content,
                "calls": [
                    {
                        "id": call.id,
                        "name": call.name,
                        "arguments": call.arguments,
                    }
                    for call in result.response.calls
                ],
                "message_extensions": result.response.message_extensions,
            },
            "usage": {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cached_input_tokens": usage.cached_input_tokens,
            },
        }
        artifact = self._projector.gateway.for_session(frame.state.session_id).save(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True)
        )
        metadata = {MODEL_USAGE: {"window": window, **manifest["usage"]}}
        control_request = self._control.take_staged_metadata(frame)
        if control_request is not None:
            metadata[CONTROL_REQUEST_METADATA] = control_request
        if result.response.message_extensions:
            metadata[MESSAGE_EXTENSIONS] = result.response.message_extensions
        if notice is not None:
            metadata[NOTICE] = notice
        if prepared.work_plan_context is not None:
            metadata[WORK_PLAN_CONTEXT] = prepared.work_plan_context
        return RecordedDecision(
            decision,
            (artifact.artifact_id,),
            metadata,
        )
