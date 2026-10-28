"""Product web capabilities, independent of user-installed MCP servers."""

from __future__ import annotations

import os
import hashlib
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from konden.llm.config import load_connections
from konden.mcp.models import McpServerRecord, StdioTransportConfig, TransportKind
from konden.paths import KondenHome


BROWSER_SERVER_IDS = frozenset({"builtin-chrome-devtools", "builtin-playwright"})
DEEPSEEK_SERVER_ID = "builtin-deepseek"
BUILTIN_SERVER_IDS = BROWSER_SERVER_IDS | {DEEPSEEK_SERVER_ID}
ROOT = Path(__file__).resolve().parents[2]


def browser_servers(task_root: Path | None = None) -> tuple[McpServerRecord, ...]:
    # Product upgrades invalidate previously loaded browser contracts as a unit.
    inputs = (Path(__file__), ROOT / "scripts/browser/package-lock.json", ROOT / "scripts/runtime_tools.json")
    content = "\n".join(path.read_text(encoding="utf-8") for path in inputs)
    revision = int.from_bytes(hashlib.sha256(content.encode()).digest()[:6]) + 1
    modules = ROOT / "scripts/browser/node_modules"
    chrome = ROOT / ".tools/runtime/chrome" / (
        "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
        if sys.platform == "darwin" else "chrome.exe" if os.name == "nt" else "chrome"
    )
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


def create_bundled_servers(home: KondenHome, task_root: Path | None = None) -> Callable[[], tuple[McpServerRecord, ...]]:
    fixed = browser_servers(task_root)
    revision = int.from_bytes(hashlib.sha256((ROOT / "konden/mcp/deepseek_web.py").read_bytes()).digest()[:6]) + 1
    deepseek = McpServerRecord(
        id=DEEPSEEK_SERVER_ID,
        display_name="DeepSeek Web Search",
        description="内置 DeepSeek 公开网页搜索与原文读取，使用已配置的官方 DeepSeek API Key，按成功请求次数计费。检索前加载 web-research Skill。",
        transport=TransportKind.STDIO,
        transport_config=StdioTransportConfig(
            command=sys.executable,
            args=("-m", "konden.mcp.deepseek_web", "--connections", str(home.connections_path)),
            cwd=str(ROOT),
        ),
        read_only=True,
        revision=revision,
    )

    def read() -> tuple[McpServerRecord, ...]:
        configured = home.connections_path.exists() and bool(
            load_connections(home.connections_path)["deepseek"]["api_key"].strip()
        )
        return (*fixed, replace(deepseek, enabled=configured))

    return read
