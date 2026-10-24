"""实测备用 Exa 检索；运行 python -m konden.mcp.check_web 可随时复查。"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from konden.mcp.bundled import search_server
from konden.mcp.client_manager import McpClientError, McpClientManager
from konden.mcp.secrets import McpSecretStore


async def check_web() -> int:
    record = search_server()
    with tempfile.TemporaryDirectory(prefix="konden-web-check-") as temporary:
        root = Path(temporary)
        try:
            async with McpClientManager(McpSecretStore(root / "secrets"), runtime_root=root / "runtime") as manager:
                searches = await manager.call_tool(record, "web_search_exa", {
                    "query": "杭州西湖博物馆官方网站开放时间",
                    "objective": "查找博物馆官方原始来源，返回开放时间与来源链接",
                    "numResults": 1,
                })
                pages = await manager.call_tool(record, "web_fetch_exa", {
                    "urls": ["https://www.westlakemuseum.com/"], "maxCharacters": 1500,
                })
        except McpClientError as exc:
            print(f"备用 Exa 检索检查失败：{exc}")
            return 2
    for label, result in (("搜索", searches), ("正文读取", pages)):
        if result.is_error or not any(
            item.type == "text" and item.text.strip() for item in result.content
        ):
            print(f"备用 Exa {label}未取得有效正文，请用浏览器检索或配置其他搜索 MCP。")
            return 2
    print("备用 Exa 检索检查通过：中文搜索与网页读取均返回内容（仅代表本次连通性，来源质量仍需核实）。")
    return 0


def main() -> int:
    return asyncio.run(check_web())


if __name__ == "__main__":
    raise SystemExit(main())
