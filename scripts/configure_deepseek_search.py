"""用现有 DeepSeek 官方密钥配置只读网页检索 MCP。"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

from konden.llm.config import load_connections
from konden.mcp.composition import build_mcp
from konden.paths import KondenHome


SERVER_ID = "deepseek-web-search"
ROOT = Path(__file__).resolve().parents[1]


async def configure(home: KondenHome) -> int:
    key = load_connections(home.connections_path)["deepseek"]["api_key"].strip()
    if not key:
        raise ValueError(f"请先在 {home.connections_path} 填写 deepseek.api_key。")
    assembly = build_mcp(home)
    async with assembly.client_manager:
        record = await assembly.service.upsert_server(
            server_id=SERVER_ID, display_name="DeepSeek Web Search",
            description="DeepSeek 公开网页搜索与原文读取。检索前加载 web-research Skill；使用官方 API Key，按成功请求次数计费。",
            transport="stdio", transport_config={
                "command": sys.executable,
                "args": ["-m", "konden.mcp.deepseek_web"],
                "cwd": str(ROOT),
                "env": {"DEEPSEEK_API_KEY": key},
            }, read_only=True,
        )
        activation = await assembly.service.test_and_enable(record.id, expected_revision=record.revision)
        if not activation.succeeded:
            print(f"检索 MCP 连接失败，保持停用：{activation.runtime.last_error_summary}", file=sys.stderr)
            return 1
    print(f"已配置并启用 {record.toolset_id}，密钥已单独保存，不影响模型选择。")
    print("连接检查已发现搜索与网页读取工具；API Key 有效性和网页可读性以实际调用结果为准。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, help="konden 数据目录；默认使用 KONDEN_HOME 或 ~/.konden")
    args = parser.parse_args(argv)
    home = KondenHome.default() if args.home is None else KondenHome(args.home)
    return asyncio.run(configure(home))


if __name__ == "__main__":
    raise SystemExit(main())
