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
from konden.search import load_search_config, search_path
from konden.media.images import image_config_path, load_image_config


BROWSER_SERVER_IDS = frozenset({"builtin-chrome-devtools", "builtin-playwright"})
DEEPSEEK_SERVER_ID = "builtin-deepseek"
OPENAI_SERVER_ID = "builtin-openai-search"
SEARCH_SERVER_IDS = frozenset({DEEPSEEK_SERVER_ID, OPENAI_SERVER_ID})
IMAGE_SERVER_ID = "builtin-image-generation"
BUILTIN_SERVER_IDS = BROWSER_SERVER_IDS | SEARCH_SERVER_IDS | {IMAGE_SERVER_ID}
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
    openai = replace(deepseek, id=OPENAI_SERVER_ID, display_name="OpenAI Web Search",
        description="内置 OpenAI 搜索模型：自动决定是否联网，返回回答、是否检索与来源。复用 OpenAI API 连接。检索前加载 web-research Skill。",
        transport_config=StdioTransportConfig(command=sys.executable,
            args=("-m", "konden.mcp.openai_web", "--connections", str(home.connections_path)), cwd=str(ROOT)))
    content = b"".join((ROOT / path).read_bytes() for path in (
        "konden/search/__init__.py", "konden/search/deepseek.py", "konden/mcp/openai_web.py"))
    image_content = b"".join((ROOT / path).read_bytes() for path in (
        "konden/media/images.py", "konden/media/openai_images.py", "konden/mcp/image_generation.py"))
    image_args = ("-m", "konden.mcp.image_generation", "--connections", str(home.connections_path))
    if task_root is not None:
        image_args += ("--output-root", str(task_root))
    images = McpServerRecord(
        id=IMAGE_SERVER_ID, display_name="Image Generation",
        description="图片生成：用文字生成图片，保存完整原图并返回预览。使用独立选择的图片模型，复用 OpenAI API 连接，按用量计费。",
        transport=TransportKind.STDIO,
        transport_config=StdioTransportConfig(command=sys.executable, args=image_args, cwd=str(ROOT)),
    )

    def read() -> tuple[McpServerRecord, ...]:
        config = load_search_config(search_path(home.connections_path))
        connections = load_connections(home.connections_path) if home.connections_path.exists() else None
        enabled = (config.enabled and connections is not None
                   and bool(connections[config.provider]["api_key"].strip()))
        signature = hashlib.sha256(content + repr(config.model_dump()).encode()).digest()
        current_revision = int.from_bytes(signature[:6]) + revision
        image_config = load_image_config(image_config_path(home.connections_path))
        image_enabled = image_config.enabled and connections is not None and bool(connections["openai"]["api_key"].strip())
        image_revision = int.from_bytes(hashlib.sha256(image_content + repr(image_config.model_dump()).encode()).digest()[:6]) + 1
        return (*fixed, replace(deepseek, enabled=enabled and config.provider == "deepseek", revision=current_revision),
                replace(openai, enabled=enabled and config.provider == "openai", revision=current_revision),
                replace(images, enabled=image_enabled, revision=image_revision))

    return read
