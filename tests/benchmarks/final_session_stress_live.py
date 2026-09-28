from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from tests.session_scheduler import build_settling_assistant as build_stress_assistant
from helperme.config import assistant_config_from_app, load_app_config
from helperme.llm.adapter import LiteLLMAdapter
from helperme.paths import HelperMeHome
from helperme.sandbox.registry import WorkspaceRegistry
from helperme.runtime import (
    CommandOutcomeReceived,
    InvokeTool,
    RuntimeStatus,
    SqliteJournal,
    StepCommitted,
    UserMessageReceived,
)


TASKS = (
    """
这是同一 Session 的第 1 个任务。读取 inputs/numbers.txt 和 inputs/brief.md；
两个读取彼此独立，请在同一个 Step 并行调用，不根据返回顺序推断含义。
根据真实内容创建 output/metrics.json 和 output/phase1.md。
metrics.json 必须是合法 JSON，包含 count、sum、min、max、sorted、codename、owner、constraint；
phase1.md 必须简洁记录计算结果和来源。完成后自行读取文件验证。
""".strip(),
    """
这是同一 Session 的第 2 个任务，必须延续上一任务的实际结果。
读取 output/metrics.json 和 output/phase1.md，在 metrics.json 中新增 average=14 和
marker=SESSION-CONTINUITY-OK，并创建 output/final_report.md。
final_report.md 必须包含 Atlas、Lin、70、14、offline-first、SESSION-CONTINUITY-OK。
修改后重新读取两个目标文件验证，不要只根据工具成功状态宣布完成。
""".strip(),
    """
这是同一 Session 的第 3 个只读收尾任务。并行核对 output/metrics.json、
output/phase1.md、output/final_report.md，并使用 grep 或 glob 做一次交叉检查。
不得修改任何文件。只有全部要求均由工具结果确认后，最终单独输出 STRESS-PASS；
否则明确输出 STRESS-FAIL 和不符合项。
""".strip(),
)


async def main() -> None:
    app_config = load_app_config()
    workspace_root = (
        Path(__file__).resolve().parents[1] / ".live_workspace"
    ).resolve()
    if not workspace_root.is_dir():
        raise AssertionError(f"missing live workspace: {workspace_root}")
    # 压测会话跑在 .live_workspace：登记进本机 registry（幂等），
    # supervisor 建会话时按归属校验。
    workspace = WorkspaceRegistry.load(
        HelperMeHome.default().workspaces_path
    ).register_path(workspace_root)

    config = assistant_config_from_app(
        app_config,
        LiteLLMAdapter(app_config.model),
    )
    delivered: list[str] = []
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    journal_path = workspace_root / ".runtime" / f"stress-{run_id}.sqlite"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal = SqliteJournal(journal_path)
    session_id = f"final-stress-{run_id}"
    assembly = await build_stress_assistant(
        config, delivered.append, journal, session_id, workspace
    )
    runtime = assembly.runtime

    try:
        async with config.llm, assembly.mcp.client_manager:
            created = await assembly.sessions.create(
                session_id, workspace.workspace_id
            )
            if not created:
                raise AssertionError("stress session was not created")
            for index, task in enumerate(TASKS, start=1):
                await assembly.sessions.receive_user_message(
                    session_id,
                    task,
                    delivery_id=f"stress-user-{index}",
                )
                await asyncio.wait_for(
                    assembly.scheduler.join(),
                    timeout=300,
                )
                state = await runtime.state(session_id)
                if state.status is not RuntimeStatus.WAITING:
                    raise AssertionError(f"task {index} stopped in {state.status}")
                if state.waiting_for != ("external_fact",):
                    raise AssertionError(
                        f"task {index} waits for {state.waiting_for}"
                    )
    finally:
        await assembly.scheduler.close()

    metrics = json.loads(
        (workspace / "output" / "metrics.json").read_text(encoding="utf-8")
    )
    expected_metrics = {
        "count": 5,
        "sum": 70,
        "min": 4,
        "max": 29,
        "sorted": [4, 8, 12, 17, 29],
        "codename": "Atlas",
        "owner": "Lin",
        "constraint": "offline-first",
        "average": 14,
        "marker": "SESSION-CONTINUITY-OK",
    }
    for key, expected in expected_metrics.items():
        if metrics.get(key) != expected:
            raise AssertionError(
                f"metrics[{key!r}]={metrics.get(key)!r}, expected {expected!r}"
            )
    phase1 = (workspace / "output" / "phase1.md").read_text(encoding="utf-8")
    report = (workspace / "output" / "final_report.md").read_text(encoding="utf-8")
    if "Atlas" not in phase1 or "70" not in phase1:
        raise AssertionError("phase1.md lacks verified phase-one facts")
    for marker in (
        "Atlas",
        "Lin",
        "70",
        "14",
        "offline-first",
        "SESSION-CONTINUITY-OK",
    ):
        if marker not in report:
            raise AssertionError(f"final_report.md lacks {marker}")
    if not any(text.strip() == "STRESS-PASS" for text in delivered):
        raise AssertionError(
            f"final delivery did not report STRESS-PASS: {delivered[-3:]}"
        )

    events = await journal.snapshot(session_id)
    if sum(isinstance(event.payload, UserMessageReceived) for event in events) != 3:
        raise AssertionError("session did not preserve all three user tasks")
    steps = [
        event.payload.step
        for event in events
        if isinstance(event.payload, StepCommitted)
    ]
    outcomes = [
        event for event in events if isinstance(event.payload, CommandOutcomeReceived)
    ]
    tool_names = {
        command.effect.name
        for step in steps
        for command in step.commands
        if isinstance(command.effect, InvokeTool) and command.effect.name != "deliver"
    }
    parallel_steps = [
        step
        for step in steps
        if sum(
            isinstance(command.effect, InvokeTool) and command.effect.name != "deliver"
            for command in step.commands
        )
        >= 2
    ]
    if not parallel_steps:
        raise AssertionError("model never issued an actual parallel tool batch")
    if len(tool_names) < 3 or len(outcomes) < 8:
        raise AssertionError(
            f"insufficient stress coverage: tools={sorted(tool_names)}, "
            f"outcomes={len(outcomes)}"
        )

    reopened = SqliteJournal(journal_path)
    replayed = await reopened.snapshot(session_id)
    if replayed != events:
        raise AssertionError("durable Journal replay differs from live history")

    print(
        json.dumps(
            {
                "ok": True,
                "session_id": session_id,
                "journal": str(journal_path),
                "events": len(events),
                "steps": len(steps),
                "outcomes": len(outcomes),
                "tool_names": sorted(tool_names),
                "parallel_steps": len(parallel_steps),
                "deliveries": len(delivered),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
