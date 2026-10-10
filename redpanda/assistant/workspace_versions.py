from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass

from redpanda.runtime import DomainFactCommitted
from redpanda.runtime.model import CommandPhase
from redpanda.sandbox.files import SandboxUnavailable, WorkspaceRestoreFailed, WorkspaceFiles, operation_id, validate_version


WORKSPACE_VERSION_FACT = "assistant.workspace_version"
WORKSPACE_RESTORE_FACT = "assistant.workspace_restore"
WORKSPACE_RESCUE_FACT = "assistant.workspace_rescue"
WORKSPACE_CARRYOVER_FACT = "assistant.workspace_carryover"


class StepNotRewindable(Exception):
    """这一步没有成功的版本记录，从它之后重开会让文件与历史对不上。"""


@dataclass(frozen=True)
class WorkspaceVersionFact:
    workspace_id: str
    step_id: str | None
    version: str | None
    error: str | None

    @classmethod
    def parse(cls, data) -> WorkspaceVersionFact:
        if not isinstance(data, Mapping) or set(data) != {
            "workspace_id", "step_id", "version", "error"
        }:
            raise ValueError("workspace version fact fields invalid")
        if type(data["workspace_id"]) is not str or not data["workspace_id"]:
            raise ValueError("workspace version workspace_id invalid")
        for key in ("step_id", "version", "error"):
            if data[key] is not None and (type(data[key]) is not str or not data[key]):
                raise ValueError(f"workspace version {key} invalid")
        _validate_version_result(data["version"], data["error"])
        return cls(**data)


def _validate_version_result(version, error):
    if (version is None) == (error is None):
        raise ValueError("workspace version must contain either version or error")
    if version is not None:
        validate_version(version)
    if error is not None and (type(error) is not str or not error):
        raise ValueError("workspace version error invalid")


def project_workspace_versions(events) -> tuple[WorkspaceVersionFact, ...]:
    return tuple(
        WorkspaceVersionFact.parse(event.payload.data)
        for event in events
        if isinstance(event.payload, DomainFactCommitted)
        and event.payload.fact_type == WORKSPACE_VERSION_FACT
    )


def workspace_version_event(events, step_id):
    """某一步的版本事实落在哪条事件上。

    从 Step 边界重开时前缀必须含这条事实，否则新身份的最后一版停在上一步，
    文件会比历史多退一格。版本记录在 Step 提交之后单独落库，不是同一条事件。
    """
    for event in events:
        if (
            isinstance(event.payload, DomainFactCommitted)
            and event.payload.fact_type == WORKSPACE_VERSION_FACT
            and WorkspaceVersionFact.parse(event.payload.data).step_id == step_id
        ):
            return event
    return None


def project_workspace_restores(events):
    restores = {}
    for event in events:
        payload = event.payload
        if not isinstance(payload, DomainFactCommitted) or payload.fact_type != WORKSPACE_RESTORE_FACT:
            continue
        data = payload.data
        if not isinstance(data, Mapping) or set(data) != {
            "command_id", "tool_call_id", "before_version", "version", "error"
        }:
            raise ValueError("workspace restore fact fields invalid")
        for key in ("command_id", "tool_call_id", "before_version"):
            if type(data[key]) is not str or not data[key]:
                raise ValueError(f"workspace restore {key} invalid")
        validate_version(data["before_version"])
        _validate_version_result(data["version"], data["error"])
        restores[data["command_id"]] = data
    return restores


def branch_baseline(events, marker, through):
    """A branch starts from the settled files, including preserved user values."""
    version = WorkspaceVersionFact.parse(marker.payload.data).version
    for event in events:
        payload = event.payload
        if marker.sequence < event.sequence <= through and isinstance(payload, DomainFactCommitted) and payload.fact_type == WORKSPACE_RESCUE_FACT:
            data = payload.data
            if not isinstance(data, Mapping) or set(data) != {"step_id", "before_version", "version", "error"}:
                raise ValueError("workspace branch baseline fields invalid")
            validate_version(data["before_version"])
            _validate_version_result(data["version"], data["error"])
            version = data["version"] if data["error"] is None else data["before_version"]
    return version


class WorkspaceVersionBoundary:
    def __init__(self, runtime, session_id: str, workspace_id: str,
                 versions: WorkspaceFiles) -> None:
        self.runtime = runtime
        self.session_id = session_id
        self.workspace_id = workspace_id
        self.versions = versions

    async def sync(self) -> None:
        events = await self.runtime.snapshot(self.session_id)
        recorded = {fact.step_id for fact in project_workspace_versions(events)}
        state = self.runtime.projector.project_visible(self.session_id, events)
        completed = [
            step for step in state.steps
            if step.step.step_id not in recorded
            and all(command.phase is CommandPhase.TERMINAL
                    or command.authorization_rejected_by_event_id is not None
                    for command in step.commands)
        ]
        if None not in recorded:
            await self._record(None, None)
        for step in completed:
            await self._record(step.step.step_id, step.committed_event_id)

    async def _record(self, step_id: str | None, cause: str | None) -> None:
        try:
            version = await self.versions.record()
        except SandboxUnavailable as exc:
            fact = WorkspaceVersionFact(self.workspace_id, step_id, None, str(exc))
        else:
            fact = WorkspaceVersionFact(self.workspace_id, step_id, version, None)
        await self.runtime.receive_domain_fact(
            self.session_id, WORKSPACE_VERSION_FACT, asdict(fact),
            delivery_id=step_id or "initial", source=WORKSPACE_VERSION_FACT,
            causation_id=cause,
        )


    async def settle_fork(self, restore, delivery_id):
        """新分支要不要连文件一起退回分支点。

        退：回到这条分支历史里最后记下的那个状态。不退：磁盘上留着本分支
        历史看不到的改动，模型必须知道，否则它会照着读到的文件往下推。
        分支点之后本来就没动过文件时两条路没有区别，也就没什么可说的。
        """
        events = await self.runtime.snapshot(self.session_id)
        recorded = [f for f in project_workspace_versions(events) if f.version is not None]
        if not recorded:
            return
        target = recorded[-1]
        marker = workspace_version_event(events, target.step_id)
        target_version = branch_baseline(events, marker, events[-1].sequence)
        current = await self.versions.record()
        if current == target_version:
            return
        if not restore:
            await self._record_rescue(delivery_id + "-baseline", target.step_id, current, current, None)
            await self.runtime.receive_domain_fact(
                self.session_id, WORKSPACE_CARRYOVER_FACT,
                {"note": "工作区文件保持现状，没有退回这条分支的历史位置；"
                         "磁盘上存在本分支历史里看不到的改动。"},
                delivery_id=delivery_id, source=WORKSPACE_CARRYOVER_FACT,
            )
            return
        try:
            restored = await self.versions.restore(target_version, identity=operation_id(self.session_id, delivery_id))
        except WorkspaceRestoreFailed as error:
            await self._record_rescue(
                delivery_id, target.step_id, error.before_version, None, str(error)
            )
            return
        await self._record_rescue(
            delivery_id, target.step_id, restored.before_version, restored.version, None
        )

    async def _record_rescue(self, delivery_id, step_id, before, version, error):
        """这条分支开始之前工作区是什么样。

        成功时不渲染：分支自己就是载体，历史和文件停在同一刻，没有可说的。
        失败时必须渲染——那时历史说在这一刻、文件不在，模型要知道这件事。
        """
        await self.runtime.receive_domain_fact(
            self.session_id, WORKSPACE_RESCUE_FACT,
            {"step_id": step_id, "before_version": before,
             "version": version, "error": error},
            delivery_id=delivery_id, source=WORKSPACE_RESCUE_FACT,
        )

    async def restore(self, command_id, target, events, visible, policy="preserve"):
        target_step = next((step for step in visible.steps
                            if any(c.command.command_id == target for c in step.commands)), None)
        if target_step is None:
            return {"ok": False, "code": "UNKNOWN_TOOL_CALL", "error": "当前上下文没有该调用。"}
        all_steps = self.runtime.projector.project_visible(self.session_id, events).steps
        position = next(i for i, step in enumerate(all_steps)
                        if step.step.step_id == target_step.step.step_id)
        previous_id = None if position == 0 else all_steps[position - 1].step.step_id
        facts = {fact.step_id: fact for fact in project_workspace_versions(events)}
        previous = facts.get(previous_id)
        if previous is None or previous.version is None:
            return {"ok": False, "code": "WORKSPACE_VERSION_UNAVAILABLE",
                    "error": "无法回退到该调用之前：那时的文件状态没有保存下来。"}
        version = branch_baseline(events, workspace_version_event(events, previous_id), target_step.sequence)
        # 恢复本身是普通操作；其执行前引用用于反向撤销实际恢复结果。
        restore = project_workspace_restores(events).get(target)
        if restore is not None:
            version = restore["before_version"]
        try:
            restored = await self.versions.restore(version, identity=operation_id(self.session_id, command_id), policy=policy)
        except WorkspaceRestoreFailed as error:
            await self._record_restore(command_id, target, error.before_version, None, str(error))
            return {"ok": False, "code": "WORKSPACE_RESTORE_FAILED", "error": str(error),
                    "hint": "文件恢复计划存在冲突，未创建恢复候选；检查用户变化后再选择策略。"}
        except SandboxUnavailable as error:
            return {"ok": False, "code": "WORKSPACE_RESTORE_FAILED", "error": str(error)}
        await self._record_restore(command_id, target, restored.before_version, restored.version, None)
        return {"ok": True, "code": "WORKSPACE_RESTORED", "tool_call_id": target}

    async def _record_restore(self, command_id, target, before, version, error):
        await self.runtime.receive_domain_fact(
            self.session_id, WORKSPACE_RESTORE_FACT,
            {"command_id": command_id, "tool_call_id": target, "before_version": before,
             "version": version, "error": error},
            delivery_id=command_id, source=WORKSPACE_RESTORE_FACT,
        )
