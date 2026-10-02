from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from uuid import uuid4

from prompt_toolkit import PromptSession
from prompt_toolkit.data_structures import Point
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.patch_stdout import patch_stdout

from helperme.assistant.attachments import AttachmentGateway
from helperme.assistant.compact.store import ConversationStatus
from helperme.assistant.host.ipc import WorkerFailed
from helperme.assistant.runner import SessionNotFoundError
from helperme.assistant.sessions import SessionView
from helperme.assistant.toolsets import ToolsetLoadError
from helperme.bootstrap import bootstrap_assistant
from helperme.model_settings import ModelConfigurationError
from helperme.channels.tui.images import ConsoleMessage, ImagePaste
from helperme.mcp.console import McpCommandError, McpConsoleAdapter
from helperme.mcp.errors import McpInputError
from helperme.skills.console import SkillCommandError, SkillConsoleAdapter
from helperme.skills.errors import SkillInputError


_INPUT_SEPARATOR = "─" * 72


class _BottomAnchoredPromptSession(PromptSession[str]):
    def __init__(self, *args, stream_output=None, **kwargs) -> None:
        self._stream_output = stream_output
        super().__init__(*args, **kwargs)

    def _create_layout(self) -> Layout:
        prompt_layout = super()._create_layout()
        prompt = HSplit(
            [prompt_layout.container],
            height=Dimension.exact(3),
        )
        stream = Window()
        if self._stream_output is not None:
            stream = Window(
                content=FormattedTextControl(
                    text=self._stream_output.render,
                    show_cursor=False,
                    get_cursor_position=self._stream_output.cursor_position,
                ),
                wrap_lines=True,
            )
        return Layout(
            HSplit([stream, prompt]),
            focused_element=prompt_layout.current_control,
        )


def _compact_tokens(tokens: int) -> str:
    if tokens < 1_000:
        return str(tokens)
    if tokens % 1_000 == 0:
        return f"{tokens // 1_000}k"
    return f"{tokens / 1_000:.1f}k"


class _StreamingConsoleOutput:
    def __init__(
        self,
        invalidate: Callable[[], None],
        write: Callable[[str], None] = print,
    ) -> None:
        self._invalidate = invalidate
        self._write = write
        self._previews: dict[tuple[str, str], str] = {}

    def render(self) -> str:
        return "\n\n".join(
            f"助手：{text}" if text else "思考中"
            for text in self._previews.values()
        )

    def cursor_position(self) -> Point:
        lines = self.render().split("\n")
        return Point(x=len(lines[-1]), y=len(lines) - 1)

    def preview(
        self,
        session_id: str,
        phase: str,
        output_id: str,
        text: str | None,
    ) -> None:
        key = (session_id, output_id)
        if phase == "started":
            self._previews[key] = ""
        elif phase == "delta":
            self._previews[key] += text
        elif phase == "aborted":
            partial = self._previews.pop(key)
            self._invalidate()
            if partial:
                self._write(f"\n助手：{partial}\n\n[输出已中止]")
            return
        else:
            raise ValueError(f"unknown preview phase: {phase}")
        self._invalidate()

    def deliver(self, session_id: str, output_id: str, text: str) -> None:
        self._previews.pop((session_id, output_id), None)
        self._invalidate()
        self._write(f"\n助手：{text}")

    def note(self, text: str) -> None:
        self._write(f"\n{text}")


class _ContextMeter:
    def __init__(self) -> None:
        self._conversation_id = ""
        self._current_session_id = ""
        self._compact_count = 0
        self._compact_phase = None
        self._used = 0
        self._compact_threshold_tokens = 0
        self._subagent_active = False

    def select(
        self,
        status: ConversationStatus,
        compact_threshold_tokens: int,
        *,
        subagent_active: bool = False,
    ) -> None:
        self._conversation_id = status.conversation_id
        self._current_session_id = status.session_id
        self._compact_count = status.compact_count
        self._compact_phase = status.compact_phase
        self._used = 0
        self._compact_threshold_tokens = compact_threshold_tokens
        self._subagent_active = subagent_active

    def update(self, session_id: str, used: int, compact_threshold_tokens: int) -> None:
        if session_id != self._conversation_id:
            return
        self._used = used
        self._compact_threshold_tokens = compact_threshold_tokens

    def update_subagent_activity(self, session_id: str, active: bool) -> None:
        if session_id == self._conversation_id:
            self._subagent_active = active

    def update_conversation_status(self, status: ConversationStatus) -> None:
        if status.conversation_id != self._conversation_id:
            return
        if status.session_id != self._current_session_id:
            self._used = 0
            self._subagent_active = False
        self._current_session_id = status.session_id
        self._compact_count = status.compact_count
        self._compact_phase = status.compact_phase

    def render(self) -> str:
        context = f"实际输入 {_compact_tokens(self._used)} / compact {_compact_tokens(self._compact_threshold_tokens)}"
        context += f"  ·  compact {self._compact_count} 次"
        if self._compact_phase is not None:
            phase = {"running": "整理中", "ready": "等待切换", "failed": "失败"}
            context += f"  ·  compact {phase[self._compact_phase]}"
        if self._subagent_active:
            context += "  ·  子 Agent 工作中"
        return f"{context}\nSession ID：{self._current_session_id}"


async def read_console_input(
    queue: asyncio.Queue[ConsoleMessage | None],
    session: PromptSession[str],
    image_paste: ImagePaste | None = None,
) -> None:
    with patch_stdout():
        while True:
            try:
                text = await session.prompt_async(
                    "你：",
                    refresh_interval=0.25,
                )
            except (EOFError, KeyboardInterrupt):
                await queue.put(None)
                return
            await queue.put(
                ConsoleMessage(text.strip())
                if image_paste is None
                else image_paste.submit(text)
            )


def _authorization_prompt(name: str, args: object) -> str:
    command = args.get("command") if isinstance(args, Mapping) else None
    detail = (
        command
        if type(command) is str
        else json.dumps(args, ensure_ascii=False)
    )
    return (
        f"命令等待授权：{name}: {detail}\n"
        "输入 yes 授权，no 拒绝。"
    )


def _print_runtime_status(view: SessionView) -> None:
    if view.control_message is not None:
        print(f"控制面：\n{view.control_message}")
    elif view.control_approval is not None:
        print(
            "控制面待确认：\n"
            f"{view.control_approval.summary}\n"
            f"风险：{view.control_approval.risk}\n"
            "输入 yes 确认，no 取消。"
        )
    print(f"Runtime 状态：{view.status}")
    if view.waiting_for:
        print("等待：" + ", ".join(view.waiting_for))
    if view.pending_authorization_ids:
        print("有命令等待授权：")
        for pending in view.pending_authorization_commands:
            command = pending.arguments.get("command")
            detail = (
                command
                if type(command) is str
                else json.dumps(pending.arguments, ensure_ascii=False)
            )
            print(f"  - {pending.name}: {detail}")
        print(
            "yes / no 写入授权事实；"
            "其他话会写成 UserMessage，由下一步模型判断。"
        )


async def run_runtime_console(workspace_path: Path | None = None) -> None:
    context_meter = _ContextMeter()
    stream_output = _StreamingConsoleOutput(lambda: session.app.invalidate())
    session: PromptSession[str] = _BottomAnchoredPromptSession(
        bottom_toolbar=context_meter.render,
        stream_output=stream_output,
    )
    async with bootstrap_assistant(
        stream_output.deliver,
        workspace_path=Path.cwd() if workspace_path is None else workspace_path,
        context_usage_sink=context_meter.update,
        subagent_activity_sink=context_meter.update_subagent_activity,
        conversation_status_sink=context_meter.update_conversation_status,
        preview_sink=stream_output.preview,
        authorization_required_sink=(
            lambda _session_id, _command_id, name, args: stream_output.note(
                _authorization_prompt(name, args)
            )
        ),
        session_failed_sink=lambda _session_id, message: stream_output.note(message),
    ) as app:
        image_paste = ImagePaste(AttachmentGateway(app.sessions_root))
        # 只追加粘贴绑定。PromptSession 自己会把它和回车提交绑在一起；
        # 再 merge load_key_bindings() 会盖掉 Enter。
        session.key_bindings = image_paste.bindings
        session.default_buffer.on_text_changed += image_paste.changed
        sessions = app.sessions

        def selected_threshold():
            profile = sessions.model_selection(session_id)["selected"]
            return 0 if profile is None else profile["compact_threshold_tokens"]
        mcp_console = McpConsoleAdapter(app.mcp_service)
        skill_console = SkillConsoleAdapter(app.skill_service)
        owner = "tui"
        session_id = f"session-{uuid4().hex}"
        workspace = app.workspace
        assert workspace is not None
        await sessions.create(session_id, workspace.workspace_id)
        view = await sessions.select(owner, session_id)
        context_meter.select(
            sessions.conversation_status(session_id),
            selected_threshold(),
            subagent_active=view.has_active_subagents,
        )
        image_paste.bind(session_id)
        input_queue: asyncio.Queue[ConsoleMessage | None] = asyncio.Queue()
        access = "整台电脑" if workspace.full_access else f"工作区 {workspace.name}"
        print(f"HelperMe 已启动。model={sessions.models.selected(session_id)}")
        print(f"工作区：{access}")
        print(f"当前对话：{session_id}")
        print("/new 新对话    /resume <id> 恢复")
        print("/mcp  /skill  管理外部能力")
        print("直接输入任务。运行中再输入会打断当前任务。")
        print("Ctrl+C 或 Ctrl+D 退出。")
        print("Ctrl+V 粘贴图片或文字；终端拦截时用 Alt+V。删除 [Image #n] 可取消附件。")

        reader = asyncio.create_task(read_console_input(input_queue, session, image_paste))
        failure = asyncio.create_task(
            app.sessions.wait_failure(),
            name="assistant-failure",
        )
        try:
            separate_turns = False
            while True:
                try:
                    if separate_turns:
                        print(f"\n{_INPUT_SEPARATOR}", flush=True)
                        separate_turns = False
                    next_input = asyncio.create_task(input_queue.get())
                    done, _ = await asyncio.wait(
                        (next_input, failure),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if failure in done:
                        error = failure.result()
                        print(f"\nSession 运行失败：{error}")
                        failure = asyncio.create_task(app.sessions.wait_failure())
                        if not next_input.done():
                            next_input.cancel()
                            await asyncio.gather(next_input, return_exceptions=True)
                            continue
                    submitted = next_input.result()
                    if submitted is None:
                        print("\n已退出。")
                        return
                    user_message = submitted.text
                    if not user_message:
                        continue
                    separate_turns = True
                    if user_message == "/new":
                        target_session_id = f"session-{uuid4().hex}"
                        await sessions.create(
                            target_session_id, workspace.workspace_id
                        )
                        view = await sessions.select(owner, target_session_id)
                        session_id = target_session_id
                        image_paste.bind(session_id)
                        context_meter.select(
                            sessions.conversation_status(session_id),
                            selected_threshold(),
                            subagent_active=view.has_active_subagents,
                        )
                        print(f"\n新 Session 已创建：{session_id}")
                        continue
                    if user_message == "/resume" or user_message.startswith("/resume "):
                        parts = user_message.split(maxsplit=1)
                        if len(parts) != 2 or not parts[1].strip():
                            print("\n用法：/resume <session_id>")
                            continue
                        target_session_id = parts[1].strip()
                        try:
                            view = await sessions.select(owner, target_session_id)
                        except SessionNotFoundError:
                            print(f"\nSession 不存在：{target_session_id}")
                            continue
                        except ToolsetLoadError as exc:
                            print(f"\nSession 恢复失败：{exc.code}: {exc.message}")
                            continue
                        session_id = target_session_id
                        image_paste.bind(session_id)
                        context_meter.select(
                            sessions.conversation_status(session_id),
                            selected_threshold(),
                            subagent_active=view.has_active_subagents,
                        )
                        print(f"\n已恢复 Session：{session_id}")
                        _print_runtime_status(view)
                        continue
                    try:
                        mcp_reply = await mcp_console.execute_if_handled(user_message)
                    except (McpCommandError, McpInputError) as exc:
                        print(f"\nMCP：{exc}")
                        continue
                    if mcp_reply is not None:
                        print(f"\nMCP：\n{mcp_reply}")
                        continue
                    try:
                        skill_reply = await skill_console.execute_if_handled(
                            user_message,
                        )
                    except (SkillCommandError, SkillInputError) as exc:
                        print(f"\nSkill：{exc}")
                        continue
                    if skill_reply is not None:
                        print(f"\nSkill：\n{skill_reply}")
                        continue
                    view = await sessions.accept_input(
                        session_id,
                        user_message,
                        artifact_refs=submitted.artifact_refs,
                        delivery_id=f"user-{uuid4().hex}",
                    )
                    if view.control_message is not None:
                        _print_runtime_status(view)
                except ModelConfigurationError as error:
                    print(f"\n模型配置：{error}")
                except WorkerFailed as error:
                    print(f"\nSession 运行失败：{error}")
        finally:
            if not failure.done():
                failure.cancel()
                await asyncio.gather(failure, return_exceptions=True)
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                pass
            await sessions.release(owner)
