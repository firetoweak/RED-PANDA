from __future__ import annotations

import argparse

from mcp.server import MCPServer


server = MCPServer("redpanda-streamable-http-fixture", log_level="ERROR")


@server.tool()
def echo(value: str) -> dict[str, str]:
    return {"value": value}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    server.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=args.port,
        streamable_http_path="/mcp",
    )


if __name__ == "__main__":
    main()
