"""Product browser capabilities, independent of user-installed MCP servers."""

from __future__ import annotations

import os
import hashlib
from pathlib import Path

from redpanda.mcp.models import McpServerRecord, StdioTransportConfig, TransportKind


BROWSER_SERVER_IDS = frozenset({"builtin-chrome-devtools", "builtin-playwright"})
ROOT = Path(__file__).resolve().parents[2]


def browser_servers(task_root: Path | None = None) -> tuple[McpServerRecord, ...]:
    # Product upgrades invalidate previously loaded browser contracts as a unit.
    inputs = (Path(__file__), ROOT / "scripts/browser/package-lock.json", ROOT / "scripts/runtime_tools.json")
    content = "\n".join(path.read_text(encoding="utf-8") for path in inputs)
    revision = int.from_bytes(hashlib.sha256(content.encode()).digest()[:6]) + 1
    modules = ROOT / "scripts/browser/node_modules"
    chrome = ROOT / ".tools/runtime/chrome" / ("chrome.exe" if os.name == "nt" else "chrome")
    return (
        McpServerRecord(
            id="builtin-chrome-devtools",
            display_name="Chrome DevTools",
            description="内置浏览器调试：查看页面、控制台和网络请求，截图与分析性能。使用独立的无头浏览器，关闭后不保留登录状态。",
            transport=TransportKind.STDIO,
            transport_config=StdioTransportConfig(
                command="node",
                cwd=None if task_root is None else str(task_root),
                args=(
                    str(modules / "chrome-devtools-mcp/build/src/bin/chrome-devtools-mcp.js"),
                    "--headless", "--isolated", "--no-usage-statistics", "--no-performance-crux",
                    f"--executable-path={chrome}",
                ),
            ),
            enabled=True,
            revision=revision,
        ),
        McpServerRecord(
            id="builtin-playwright",
            display_name="Playwright",
            description="内置浏览器自动化：按页面快照操作网页、填写表单、截图并验证用户流程。使用独立的无头浏览器，关闭后不保留登录状态。",
            transport=TransportKind.STDIO,
            transport_config=StdioTransportConfig(
                command="node",
                cwd=None if task_root is None else str(task_root),
                args=(
                    str(modules / "@playwright/mcp/cli.js"),
                    "--headless", "--isolated", f"--executable-path={chrome}",
                ),
            ),
            enabled=True,
            revision=revision,
        ),
    )
