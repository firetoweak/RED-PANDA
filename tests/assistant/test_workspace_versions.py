import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from redpanda.assistant.context.projection import project_chat_messages
from redpanda.assistant.workspace_versions import (
    WORKSPACE_CARRYOVER_FACT, WorkspaceVersionBoundary, WorkspaceVersionFact,
    project_workspace_versions, workspace_version_event,
    operation_id,
)
from redpanda.runtime import AgentRuntime, InvokeTool, MemoryJournal, ModelDecision, ToolBinding
from tests.assistant.test_runner import ScriptedDecisionMaker
from tests.session_scheduler import SettlingScheduler
from redpanda.sandbox.files.operations import SandboxUnavailable


def test_record_waits_for_whole_batch_without_context_noise_or_extra_decision():
    async def scenario():
        first_done, release_second = asyncio.Event(), asyncio.Event()
        async def first(*_):
            first_done.set()
            return {"ok": True, "code": "DONE"}
        async def second(*_):
            await release_second.wait()
            return {"ok": True, "code": "DONE"}
        model = ScriptedDecisionMaker((
            lambda _: ModelDecision(command_requests=(InvokeTool("first"), InvokeTool("second"))),
            lambda _: ModelDecision(content="done"),
        ))
        runtime = AgentRuntime(MemoryJournal(), model,
                               {"first": ToolBinding(first), "second": ToolBinding(second)})
        await runtime.create_session("s")
        versions = AsyncMock()
        versions.record.side_effect = ["a" * 64, "b" * 64, "b" * 64]
        boundary = WorkspaceVersionBoundary(runtime, "s", "workspace-test", versions)
        scheduler = SettlingScheduler(runtime, "s")
        scheduler.record_workspace_versions = boundary.sync
        try:
            await runtime.receive_user_message("s", "go", delivery_id="go")
            await scheduler.wake("s")
            await asyncio.wait_for(first_done.wait(), 2)
            await boundary.sync()
            assert versions.record.await_count == 1
            release_second.set()
            await scheduler.join()
            events = await runtime.snapshot("s")
            facts = project_workspace_versions(events)
            assert [fact.version for fact in facts] == ["a" * 64, "b" * 64, "b" * 64]
            assert len(model.frames) == 2
            frame = model.frames[1]
            messages = project_chat_messages(
                tuple(e for e in events if e.sequence <= frame.observed_journal_position),
                frame.state,
            )
            assert "assistant.workspace_version" not in json.dumps(messages)
            assert "b" * 64 not in json.dumps(messages)
            # 新的边界实例只从 Journal 恢复，不再记录旧 Step。
            await WorkspaceVersionBoundary(runtime, "s", "workspace-test", versions).sync()
            assert versions.record.await_count == 3
        finally:
            await scheduler.close()
    asyncio.run(scenario())


def test_record_failure_is_a_visible_fact_but_unknown_error_propagates():
    async def scenario():
        runtime = AgentRuntime(MemoryJournal(), ScriptedDecisionMaker(()), {})
        await runtime.create_session("s")
        versions = AsyncMock()
        versions.record.side_effect = SandboxUnavailable("native program missing")
        boundary = WorkspaceVersionBoundary(runtime, "s", "workspace-test", versions)
        await boundary.sync()
        facts = project_workspace_versions(await runtime.snapshot("s"))
        assert facts[0].version is None
        assert facts[0].error == "native program missing"
        await runtime.create_session("other")
        versions.record.side_effect = RuntimeError("corrupt repository")
        with pytest.raises(RuntimeError, match="corrupt"):
            await WorkspaceVersionBoundary(runtime, "other", "workspace-test", versions).sync()
        assert not await runtime.snapshot("other")
    asyncio.run(scenario())


async def restore_history(recorded):
    from redpanda.sandbox.files.operations import WorkspaceRestore
    async def tool(*_):
        return {"ok": True, "code": "DONE"}
    model = ScriptedDecisionMaker((
        lambda _: ModelDecision(command_requests=(InvokeTool("write"), InvokeTool("write"))),
        lambda _: ModelDecision(command_requests=(InvokeTool("write"),)),
        lambda _: ModelDecision(content="done"),
    ))
    runtime = AgentRuntime(MemoryJournal(), model, {"write": ToolBinding(tool)})
    await runtime.create_session("s")
    versions = AsyncMock()
    versions.record.side_effect = recorded
    versions.restore.return_value = WorkspaceRestore("f" * 64, "e" * 64)
    boundary = WorkspaceVersionBoundary(runtime, "s", "workspace-test", versions)
    scheduler = SettlingScheduler(runtime, "s")
    scheduler.record_workspace_versions = boundary.sync
    try:
        await runtime.receive_user_message("s", "go", delivery_id="go")
        await scheduler.wake("s")
        await scheduler.join()
    finally:
        await scheduler.close()
    events = await runtime.snapshot("s")
    visible = runtime.projector.project_visible("s", events)
    return runtime, boundary, versions, events, visible


@pytest.mark.parametrize("step_index,command_index,expected", [(0, 0, "a"), (0, 1, "a"), (1, 0, "b")])
def test_call_resolves_to_previous_step_not_its_own_snapshot(step_index, command_index, expected):
    async def scenario():
        runtime, boundary, versions, events, visible = await restore_history(["a" * 64, "b" * 64, "c" * 64, "c" * 64])
        target = visible.steps[step_index].commands[command_index].command.command_id
        result = await boundary.restore("restore", target, events, visible)
        assert result == {"ok": True, "code": "WORKSPACE_RESTORED", "tool_call_id": target}
        versions.restore.assert_awaited_once_with(expected * 64, identity=operation_id("s", "restore"), policy="preserve")
        all_events = await runtime.snapshot("s")
        messages = project_chat_messages(all_events, runtime.projector.project_visible("s", all_events))
        assert "before_version" not in json.dumps(messages)
        assert "assistant.workspace_version" not in json.dumps(messages)
    asyncio.run(scenario())


@pytest.mark.parametrize("failed", [0, 1])
def test_missing_previous_snapshot_does_not_fall_back(failed):
    async def scenario():
        records = ["a" * 64, "b" * 64, "c" * 64, "c" * 64]
        records[failed] = SandboxUnavailable("snapshot failed")
        runtime, boundary, versions, events, visible = await restore_history(records)
        target = visible.steps[failed].commands[0].command.command_id
        result = await boundary.restore("restore", target, events, visible)
        assert result["code"] == "WORKSPACE_VERSION_UNAVAILABLE"
        versions.restore.assert_not_awaited()
        messages = project_chat_messages(events, visible)
        assert "snapshot failed" in json.dumps(messages)
        assert "a" * 64 not in json.dumps(messages)
    asyncio.run(scenario())


def test_compact_window_cannot_reference_removed_calls():
    async def scenario():
        from redpanda.assistant.compact.core import CompactContext
        from redpanda.assistant.context.projection import ModelContextProjector
        runtime, boundary, versions, events, visible = await restore_history(["a" * 64] * 4)
        context = CompactContext("s", events, ModelContextProjector(), None)
        # 模拟已发布窗口；实际 visible() 仍负责选择可见调用。
        from unittest.mock import patch
        context.window = {"cutover": visible.steps[1].sequence - 1}
        with patch.object(context, "refresh"):
            window = context.visible(events, visible)
        target = visible.steps[0].commands[0].command.command_id
        result = await boundary.restore("restore", target, events, window)
        assert result["code"] == "UNKNOWN_TOOL_CALL"
        versions.restore.assert_not_awaited()
    asyncio.run(scenario())


def test_partial_restore_retains_rescue_fact_without_exposing_version_addresses():
    async def scenario():
        from redpanda.sandbox.files.operations import WorkspaceRestoreFailed
        from redpanda.assistant.workspace_versions import WORKSPACE_RESTORE_FACT
        runtime, boundary, versions, events, visible = await restore_history(["a" * 64] * 4)
        versions.restore.side_effect = WorkspaceRestoreFailed("f" * 64, OSError("disk full"))
        target = visible.steps[0].commands[0].command.command_id
        result = await boundary.restore("restore", target, events, visible)
        assert result["ok"] is False
        assert "f" * 64 not in json.dumps(result)
        saved = (await runtime.snapshot("s"))[-1].payload
        assert saved.fact_type == WORKSPACE_RESTORE_FACT
        assert saved.data["before_version"] == "f" * 64
        versions.restore.side_effect = RuntimeError("corrupt")
        with pytest.raises(RuntimeError, match="corrupt"):
            await boundary.restore("other", target, events, visible)
    asyncio.run(scenario())


@pytest.mark.parametrize("step_index,expected", [(0, "b"), (1, "c")])
def test_the_restart_boundary_is_the_steps_own_version_fact(step_index, expected):
    """从第 N 步重开，前缀要含这一步自己的版本事实。

    版本是在 Step 提交之后单独落库的。切在提交那条事件上，新身份记住的
    最后一版是上一步的，文件会比历史多退一格——人指的是「回到这一刻」，
    比模型的「撤销这次调用」晚一格。
    """
    async def scenario():
        _, _, _, events, visible = await restore_history(
            ["a" * 64, "b" * 64, "c" * 64, "c" * 64]
        )
        step = visible.steps[step_index].step
        boundary = workspace_version_event(events, step.step_id)
        prefix = [e for e in events if e.sequence <= boundary.sequence]
        assert project_workspace_versions(prefix)[-1].version == expected * 64
    asyncio.run(scenario())


def test_a_step_whose_version_was_never_taken_has_no_boundary_to_guess_at():
    async def scenario():
        _, _, _, events, visible = await restore_history(
            ["a" * 64, SandboxUnavailable("snapshot failed"), "c" * 64, "c" * 64]
        )
        boundary = workspace_version_event(events, visible.steps[0].step.step_id)
        assert WorkspaceVersionFact.parse(boundary.payload.data).version is None
    asyncio.run(scenario())


def test_fork_only_speaks_up_when_the_files_actually_diverged():
    """分支点之后没动过文件时，退与不退没有区别，不该往上下文里塞话。

    真有改动而用户选了不退，模型必须知道磁盘上有本分支历史看不到的东西。
    """
    async def scenario():
        runtime, boundary, versions, events, _ = await restore_history(
            ["a" * 64, "b" * 64, "c" * 64, "c" * 64]
        )
        branch_point = project_workspace_versions(events)[-1].version
        versions.record.side_effect = None

        versions.record.return_value = branch_point
        await boundary.settle_fork(False, "edit-1-workspace")
        assert (await runtime.snapshot("s"))[-1].payload.fact_type != WORKSPACE_CARRYOVER_FACT

        versions.record.return_value = "d" * 64
        await boundary.settle_fork(False, "edit-2-workspace")
        saved = (await runtime.snapshot("s"))[-1].payload
        assert saved.fact_type == WORKSPACE_CARRYOVER_FACT
        versions.restore.assert_not_awaited()
        all_events = await runtime.snapshot("s")
        messages = project_chat_messages(
            all_events, runtime.projector.project_visible("s", all_events)
        )
        assert WORKSPACE_CARRYOVER_FACT in json.dumps(messages)

        await boundary.settle_fork(True, "edit-3-workspace")
        # 留在磁盘上的状态已经成为本分支起点，重试不再改文件。
        versions.restore.assert_not_awaited()
    asyncio.run(scenario())


def test_undoing_restore_uses_its_saved_pre_restore_files():
    async def scenario():
        runtime, boundary, versions, events, visible = await restore_history(["a" * 64, "b" * 64, "c" * 64, "c" * 64])
        original = visible.steps[0].commands[0].command.command_id
        restore_call = visible.steps[1].commands[0].command.command_id
        await boundary._record_restore(restore_call, original, "f" * 64, None, "restore interrupted")
        events = await runtime.snapshot("s")
        result = await boundary.restore("undo-restore", restore_call, events, visible)
        assert result["ok"] is True
        versions.restore.assert_awaited_once_with("f" * 64, identity=operation_id("s", "undo-restore"), policy="preserve")
    asyncio.run(scenario())
