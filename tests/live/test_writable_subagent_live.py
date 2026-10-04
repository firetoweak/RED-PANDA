"""真实模型、父子 Worker、MCP stdio、授权与 Git 文件合入的单次闭环。"""
import asyncio
import json
import os
import subprocess
import sys
import shutil
from uuid import uuid4

import pytest

from redpanda.assistant.subagent.subagent import project_delegate_intents, project_reclaimed
from redpanda.assistant.subagent.workspace import child_layout
from redpanda.assistant.workspace_versions import project_workspace_versions
from redpanda.bootstrap import bootstrap_assistant
from redpanda.config import load_app_config
from redpanda.mcp.adapter import encode_tool_name
from redpanda.paths import RedPandaHome
from redpanda.runtime import CommandOutcomeReceived, DomainFactCommitted, InvokeTool, SqliteJournal, StepCommitted


pytestmark = [pytest.mark.live, pytest.mark.skipif(
    os.environ.get("REDPANDA_RUN_LIVE_TESTS") != "1", reason="需显式启用 live 测试",
)]


def test_real_model_delegates_loads_readonly_mcp_and_merges_only_after_authorization(tmp_path, monkeypatch):
    app_config = load_app_config()
    connections_path = RedPandaHome.default().connections_path
    home_root = tmp_path / "home"
    root = tmp_path / "project"
    root.mkdir()
    protected = root / "protected.txt"
    protected.write_text("parent-only\n", encoding="utf-8")
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=E2E", "-c", "user.email=e2e@local",
                    "commit", "-m", "baseline"], check=True, capture_output=True)
    user_head = (root / ".git" / "HEAD").read_bytes()
    user_index = (root / ".git" / "index").read_bytes()
    monkeypatch.setenv("REDPANDA_HOME", str(home_root))
    home_root.mkdir(exist_ok=True)
    shutil.copyfile(connections_path, home_root / "connections.json")
    nonce = "E2E_" + uuid4().hex
    server = home_root / "readonly_server.py"
    server.write_text(
        'from mcp.server import MCPServer\n'
        'server = MCPServer("e2e-readonly")\n'
        '@server.tool()\n'
        'def read_probe() -> dict[str, str]:\n'
        '    """返回本次测试需要写入 result.txt 的随机标记。"""\n'
        f'    return {{"marker": {nonce!r}}}\n'
        'if __name__ == "__main__":\n'
        '    server.run()\n', encoding="utf-8",
    )

    async def scenario():
        authorization = asyncio.Event()
        completed = asyncio.Event()
        approvals = []
        outputs = []
        known_failures = []
        progress = []

        def sink(session_id, output_id, text):
            outputs.append((session_id, text))
            print(f"delivery: {session_id}: {text}", flush=True)
            if "E2E_COMPLETED" in text:
                completed.set()

        def request_authorization(session_id, command_id, name, arguments):
            approvals.append((session_id, command_id, name))
            authorization.set()
            print(f"authorization requested: {name}", flush=True)

        def tool_progress(session_id, phase, command_id, name, data):
            progress.append((session_id, phase, name))
            if phase == "finish":
                print(f"tool completed: {session_id}: {name}", flush=True)

        async with bootstrap_assistant(
            sink, app_config=app_config, workspace_path=root,
            authorization_required_sink=request_authorization,
            tool_progress_sink=tool_progress,
            session_failed_sink=lambda session_id, text: known_failures.append((session_id, text)),
        ) as app:
            host = app.sessions
            for server_id, read_only in (("e2e_reader", True), ("e2e_undeclared", False)):
                record = await app.mcp_service.upsert_server(
                    server_id=server_id, display_name=server_id, description="端到端测试能力",
                    transport="stdio", transport_config={"command": sys.executable, "args": [str(server)]},
                    read_only=read_only,
                )
                activation = await app.mcp_service.test_and_enable(record.id, expected_revision=record.revision)
                assert activation.succeeded
            print(f"real model: {app.config.default_model}", flush=True)
            await host.create("e2e-parent", app.workspace.workspace_id)
            failure = asyncio.create_task(host.wait_failure())

            async def wait_for(event):
                waiting = asyncio.create_task(event.wait())
                try:
                    async with asyncio.timeout(300):
                        done, _ = await asyncio.wait((waiting, failure), return_when=asyncio.FIRST_COMPLETED)
                    if failure in done:
                        raise failure.result()
                    assert not known_failures, known_failures
                finally:
                    waiting.cancel()
                    await asyncio.gather(waiting, return_exceptions=True)

            try:
                child_task = (
                    "你在进行端到端测试。先 load_toolset 加载 mcp:e2e_reader，再调用它的 read_probe，"
                    "取得随机 marker。必须真实调用，不能猜。然后使用 write_file 将 marker 加一个换行写入"
                    "相对路径 result.txt。另用 write_file 尝试覆盖父目录的绝对路径 "
                    f"{protected.as_posix()}，content=illegal，overwrite=true；这次必须被路径边界拒绝。"
                    "用 read_file 核对自己的 result.txt。最后 report，说明写入内容和越界写被拒绝的事实。"
                    "不要执行命令，不要修改其他文件。"
                )
                prompt = (
                    "请完成一次文件合入端到端测试。严格只委派一个子 Agent，任务描述逐字使用："
                    + child_task + "\n等待它回传；你自己不要写文件。收到回传后，先 compare_subagent 列文件，"
                    "再指定 paths=[result.txt] 获取 diff 验收。然后单独调用 merge_subagent；测试程序会"
                    "对该合入请求授权，不必用正文追问。授权合入成功后 read_file 读取 result.txt 并核对"
                    "marker，再 get_changes 验证实际变化，最后回复 E2E_COMPLETED 和该 marker。"
                    "不要调用 execute_command，不要改变 Git，不要管理或安装能力。"
                )
                await host.receive_user_message("e2e-parent", prompt, delivery_id="e2e-input")
                await wait_for(authorization)
                assert len(approvals) == 1 and approvals[0][2] == "merge_subagent", approvals
                assert not (root / "result.txt").exists(), "授权前子改动已泄漏到父工作树"
                assert protected.read_text(encoding="utf-8") == "parent-only\n"
                parent_events = await SqliteJournal(host.store.require("e2e-parent")).snapshot("e2e-parent")
                intents = project_delegate_intents(parent_events)
                assert len(intents) == 1
                child_id = intents[0].child_session_id
                child_root, _ = child_layout(host.home, "e2e-parent", child_id)
                assert (child_root / "result.txt").read_text(encoding="utf-8") == nonce + "\n"
                assert project_reclaimed(parent_events) == {child_id}
                assert all(session_id == "e2e-parent" for session_id, _ in outputs)
                await host.resolve_authorization("e2e-parent", approvals[0][1], approved=True)
                await wait_for(completed)
                assert (root / "result.txt").read_text(encoding="utf-8") == nonce + "\n"
                assert protected.read_text(encoding="utf-8") == "parent-only\n"
                assert (root / ".git" / "HEAD").read_bytes() == user_head
                assert (root / ".git" / "index").read_bytes() == user_index
                child_events = await SqliteJournal(host.store.require(child_id)).snapshot(child_id)
                versions = project_workspace_versions(child_events)
                assert versions and all(fact.version is not None for fact in versions)
                catalogs = [event.payload.data for event in child_events if isinstance(event.payload, DomainFactCommitted)
                            and event.payload.fact_type == "assistant.catalog"]
                assert {item["id"] for item in catalogs[-1]["toolsets"]} == {"mcp:e2e_reader"}
                child_calls = {command.command_id: command.effect for event in child_events
                               if isinstance(event.payload, StepCommitted)
                               for command in event.payload.step.commands if isinstance(command.effect, InvokeTool)}
                assert encode_tool_name("e2e_reader", "read_probe") in {call.name for call in child_calls.values()}
                assert all(call.name != "execute_command" for call in child_calls.values())
                assert any(event.payload.outcome.value.get("code") == "PATH_OUTSIDE_WORKSPACE_VIEW"
                           for event in child_events if isinstance(event.payload, CommandOutcomeReceived)
                           and event.payload.command_id in child_calls)
                parent_events = await SqliteJournal(host.store.require("e2e-parent")).snapshot("e2e-parent")
                outcomes = {event.payload.command_id: event.payload.outcome for event in parent_events
                            if isinstance(event.payload, CommandOutcomeReceived)}
                assert outcomes[approvals[0][1]].value["ok"] is True
                report = {"model": app.config.default_model, "marker": nonce, "child": child_id,
                          "authorization": approvals, "tools": progress, "outputs": outputs}
                (tmp_path / "e2e-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print("E2E assertions passed; evidence:", tmp_path, flush=True)
            finally:
                failure.cancel()
                await asyncio.gather(failure, return_exceptions=True)

    asyncio.run(scenario())
