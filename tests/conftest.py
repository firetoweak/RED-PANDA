from __future__ import annotations

import os
from pathlib import Path

import pytest


PROCESS_GROUP_PATHS = {
    "compact": ("assistant/test_compact.py",),
    "supervisor": ("assistant/test_supervisor.py",),
    "execution": (
        "execution/",
        "tools/test_command_execution.py",
        "tools/test_command_io_errors.py",
    ),
}


collect_ignore = []
if os.environ.get("REDPANDA_RUN_LIVE_TESTS") != "1":
    collect_ignore.append("live")


def pytest_addoption(parser):
    parser.addoption(
        "--all",
        action="store_true",
        default=False,
        help="也包括 process 层（真实进程、shell、MCP 传输）",
    )
    parser.addoption(
        "--process-group",
        choices=(*PROCESS_GROUP_PATHS, "integration"),
        help="只运行指定 process 测试组；integration 包含其余所有 process 用例",
    )


def pytest_collection_modifyitems(config, items):
    group = config.getoption("--process-group")
    if group:
        root = Path(__file__).parent
        selected, deselected = [], []
        for item in items:
            relative = item.path.relative_to(root).as_posix()
            # The most specific path wins, so a file can be carved out of a directory group.
            item_group = max(((len(path), name) for name, paths in PROCESS_GROUP_PATHS.items()
                              for path in paths if relative.startswith(path)),
                             default=(0, "integration"))[1]
            if item.get_closest_marker("process") and item_group == group:
                selected.append(item)
            else:
                deselected.append(item)
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected
        return
    if config.getoption("--all"):
        return
    if (config.option.markexpr or "").strip():
        return
    process_items = [item for item in items if item.get_closest_marker("process")]
    if not process_items or len(process_items) == len(items):
        return
    config.hook.pytest_deselected(items=process_items)
    items[:] = [item for item in items if item.get_closest_marker("process") is None]


@pytest.fixture(autouse=True)
def _isolate_redpanda_home(monkeypatch):
    """测试进程可能由一个设了 REDPANDA_HOME 的 RED PANDA 实例派生。"""
    monkeypatch.delenv("REDPANDA_HOME", raising=False)


def pytest_report_header(config):
    if group := config.getoption("--process-group"):
        layer = f"process / {group}"
    elif config.getoption("--all"):
        layer = "all（default + process）"
    elif config.option.markexpr == "process":
        layer = "process"
    elif config.option.markexpr == "live":
        layer = "live"
    else:
        layer = "default（混合收集时不含 process）"
    return f"redpanda tests: {layer}"
