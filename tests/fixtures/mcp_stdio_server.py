from __future__ import annotations

import os

from mcp.server import MCPServer


server = MCPServer("redpanda-test-server")
counter = 0


@server.tool()
def read_test_token() -> dict[str, str]:
    """返回测试进程收到的 MCP_TEST_TOKEN。"""
    return {"token": os.environ.get("MCP_TEST_TOKEN", "")}


@server.tool()
def increment_counter() -> dict[str, int]:
    """递增并返回当前 MCP Server 进程内计数。"""
    global counter
    counter += 1
    return {"count": counter}


@server.tool()
def read_working_directory() -> dict[str, str]:
    """返回 MCP Server 进程的当前工作目录。"""
    return {"cwd": os.getcwd()}


if __name__ == "__main__":
    server.run()
