from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Literal

from redpanda.runtime import DomainFactCommitted, StepCommitted
from redpanda.runtime.model import CommandPhase
from redpanda.filesystem import FileSystemUnavailable, WorkspaceRestoreFailed, WorkspaceFiles, operation_id, validate_version


WORKSPACE_VERSION_FACT = "assistant.workspace_version"
WORKSPACE_RESTORE_FACT = "assistant.workspace_restore"
WORKSPACE_RESCUE_FACT = "assistant.workspace_rescue"


async def session_file_operations(journal, events) -> tuple[str, ...]:
    """Resolve file-operation owners, including the inherited branch prefix."""
    if not events:
        return ()
    spans = await journal.history_through(events[-1].sequence)
    return tuple(
        operation_id(span.session_id, command.command_id)
        for span in spans
        for event in events
        if span.first_sequence <= event.sequence <= span.last_sequence and isinstance(event.payload, StepCommitted)
        for command in event.payload.step.commands
    )


SEMANTIC_RESTORE_HINT = (
    "机械回退不保证符合用户意图。请读取相关文件的当前内容，核对已有修改证据；"
    "若仍需调整，按用户目标使用普通文件工具修改，并遵守现有写入授权。"
    "不要自动升级回退策略或反复重试同一次失败。"
)


@dataclass(frozen=True)
class WorkspaceFileResult:
    policy: Literal["preserve", "original", "keep"]
    effect: Literal["restored", "partially_restored", "preserved", "unchanged", "kept", "failed"]
    changed_files: tuple[str, ...] = ()
    preserved_files: tuple[str, ...] = ()
    deleted_files: tuple[str, ...] = ()
    failed_files: tuple[str, ...] = ()
    error: str | None = None

    @classmethod
    def parse(cls, data):
        if not isinstance(data, Mapping) or set(data) != {
            "policy", "effect", "changed_files", "preserved_files", "deleted_files", "failed_files", "error"
        }:
            raise ValueError("workspace file result fields invalid")
        if data["policy"] not in ("preserve", "original", "keep"):
            raise ValueError("workspace file result policy invalid")
        if data["effect"] not in ("restored", "partially_restored", "preserved", "unchanged", "kept", "failed"):
            raise ValueError("workspace file result effect invalid")
        paths = {}
        for key in ("changed_files", "preserved_files", "deleted_files", "failed_files"):
            values = data[key]
            if not isinstance(values, (list, tuple)) or any(type(path) is not str or not path for path in values):
                raise ValueError(f"workspace file result {key} invalid")
            paths[key] = tuple(values)
        error = data["error"]
        if (data["effect"] == "failed") != (type(error) is str and bool(error)):
            raise ValueError("workspace file result error invalid")
        if data["effect"] != "failed" and error is not None:
            raise ValueError("workspace file result error invalid")
        return cls(data["policy"], data["effect"], **paths, error=error)


def restored_file_result(restored, policy):
    changed, preserved = restored.changed_paths, restored.preserved_paths
    effect = ("partially_restored" if changed and preserved else "restored" if changed
              else "preserved" if preserved else "unchanged")
    return WorkspaceFileResult(policy, effect, changed, preserved, restored.deleted_paths)


def workspace_file_result_note(result):
    note = {
        "failed": "会话已回退，文件未能恢复；本次文件回退没有修改文件。",
        "partially_restored": "文件已部分回退，同时保留了后续值；当前文件可能与保留的对话记录不同。",
        "preserved": "文件回退保留了后续值，本次没有修改文件；当前文件可能与保留的对话记录不同。",
        "restored": "已执行文件回退，请核对实际修改和删除的文件。",
        "unchanged": "本次文件处理没有改变文件。",
        "kept": "工作区文件保持现状，没有退回这条分支的历史位置；存在保留对话之外的文件变化。",
    }[result.effect]
    if result.policy == "original":
        note += "原值策略允许覆盖助手改变位置的后续值，或删除助手创建后又被修改的文件。"
    return note + SEMANTIC_RESTORE_HINT


def project_workspace_file_result(events):
    result = None
    for event in events:
        payload = event.payload
        if isinstance(payload, DomainFactCommitted) and payload.fact_type == WORKSPACE_RESCUE_FACT:
            result = workspace_rescue_result(payload.data)
    return result


def workspace_rescue_result(data):
    if not isinstance(data, Mapping) or set(data) != {"step_id", "before_version", "version", "error", "result"}:
        raise ValueError("workspace branch baseline fields invalid")
    if data["step_id"] is not None and (type(data["step_id"]) is not str or not data["step_id"]):
        raise ValueError("workspace branch step_id invalid")
    validate_version(data["before_version"])
    _validate_version_result(data["version"], data["error"])
    result = WorkspaceFileResult.parse(data["result"])
    if result.error != data["error"]:
        raise ValueError("workspace branch result differs from boundary")
    return result


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
            workspace_rescue_result(data)
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
        except FileSystemUnavailable as exc:
            fact = WorkspaceVersionFact(self.workspace_id, step_id, None, "当前无法记录工作区文件状态，请让用户检查应用安装。")
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
        分支点之后没动过文件时两条路没有区别，结果明确记为未改变。
        """
        events = await self.runtime.snapshot(self.session_id)
        for event in events:
            if (event.delivery is not None
                    and event.delivery.source == WORKSPACE_RESCUE_FACT
                    and event.delivery.delivery_id == delivery_id):
                result = workspace_rescue_result(event.payload.data)
                if result.policy != ("preserve" if restore else "keep"):
                    raise ValueError("workspace settlement identity was reused")
                return
        recorded = [f for f in project_workspace_versions(events) if f.version is not None]
        if not recorded:
            return
        target = recorded[-1]
        marker = workspace_version_event(events, target.step_id)
        target_version = branch_baseline(events, marker, events[-1].sequence)
        current = await self.versions.record()
        if current == target_version:
            await self._record_rescue(delivery_id, target.step_id, current, current, None,
                                      WorkspaceFileResult("preserve" if restore else "keep", "unchanged"))
            return
        if not restore:
            await self._record_rescue(delivery_id, target.step_id, current, current, None,
                                      WorkspaceFileResult("keep", "kept"))
            return
        try:
            restored = await self.versions.restore(target_version, identity=operation_id(self.session_id, delivery_id))
        except WorkspaceRestoreFailed as error:
            await self._record_rescue(
                delivery_id, target.step_id, error.before_version, None, str(error),
                WorkspaceFileResult("preserve", "failed", failed_files=error.paths, error=str(error)),
            )
            return
        except FileSystemUnavailable:
            error = "当前无法回退工作区文件，请让用户检查应用安装。"
            await self._record_rescue(delivery_id, target.step_id, current, None, error,
                                      WorkspaceFileResult("preserve", "failed", error=error))
            return
        await self._record_rescue(
            delivery_id, target.step_id, restored.before_version, restored.version, None,
            restored_file_result(restored, "preserve"),
        )

    async def _record_rescue(self, delivery_id, step_id, before, version, error, result):
        """这条分支开始之前工作区是什么样。

        文件边界和实际处理结果来自同一次操作，模型只读取可观察的结果。
        """
        await self.runtime.receive_domain_fact(
            self.session_id, WORKSPACE_RESCUE_FACT,
            {"step_id": step_id, "before_version": before,
             "version": version, "error": error, "result": asdict(result)},
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
                    "failed_files": list(error.paths),
                    "hint": "本次回退没有修改文件。" + SEMANTIC_RESTORE_HINT}
        except FileSystemUnavailable as error:
            return {"ok": False, "code": "WORKSPACE_RESTORE_FAILED",
                    "error": "当前无法回退工作区文件，请让用户检查应用安装。",
                    "hint": SEMANTIC_RESTORE_HINT}
        await self._record_restore(command_id, target, restored.before_version, restored.version, None)
        result = restored_file_result(restored, policy)
        return {"ok": True, "code": "WORKSPACE_RESTORED", "tool_call_id": target,
                "data": asdict(result), "hint": workspace_file_result_note(result)}

    async def _record_restore(self, command_id, target, before, version, error):
        await self.runtime.receive_domain_fact(
            self.session_id, WORKSPACE_RESTORE_FACT,
            {"command_id": command_id, "tool_call_id": target, "before_version": before,
             "version": version, "error": error},
            delivery_id=command_id, source=WORKSPACE_RESTORE_FACT,
        )
