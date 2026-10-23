"""Create or revoke a local, Bot-scoped external collaborator credential."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from redpanda.assistant.external_messages import ConnectionStore
from redpanda.assistant.host.agent_roster import AgentRoster
from redpanda.assistant.host.session_store import SessionStore
from redpanda.paths import RedPandaHome


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, help="RED PANDA 的个人数据目录")
    parser.add_argument("--name", help="外部协作者显示名，例如 Codex")
    parser.add_argument("--bot", help="允许联系的现有 Bot 名字")
    parser.add_argument("--url", default="http://127.0.0.1:8765", help="运行中的 RED PANDA Web 地址")
    parser.add_argument("--revoke", help="撤销此前输出的 connection_id；立即生效")
    args = parser.parse_args()
    home = RedPandaHome.default() if args.home is None else RedPandaHome(args.home)
    store = ConnectionStore(home.root / "external_connections.json")
    if args.revoke is not None:
        store.revoke(args.revoke)
        print("已撤销接入。")
        return
    if not args.name or not args.name.strip() or not args.bot:
        parser.error("创建接入需要 --name 和 --bot")
    agent = await AgentRoster(SessionStore(home.runtime_sessions_root)).resolve(args.bot)
    connection, token = store.create(args.name, agent.session_id)
    print(json.dumps({"connection_id": connection.connection_id,
        "mcpServers": {"red-panda": {"url": args.url.rstrip("/") + "/mcp/",
            "headers": {"Authorization": "Bearer " + token}}}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
