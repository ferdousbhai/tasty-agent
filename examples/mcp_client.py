"""Connect to a remote tasty-agent MCP server and call tools.

Usage:
    # List tools
    uv run examples/mcp_client.py

    # Call a specific tool
    uv run examples/mcp_client.py market_status
    uv run examples/mcp_client.py get_market_metrics '{"symbols": ["AAPL", "SPY"]}'
    uv run examples/mcp_client.py account_overview '{"include": ["balances", "positions"]}'

Environment:
    TASTY_AGENT_MCP_URL   - MCP server URL (default: http://localhost:8000/mcp)
    MODAL_KEY             - Modal proxy auth key (optional)
    MODAL_SECRET          - Modal proxy auth secret (optional)
"""

import asyncio
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()

MCP_URL = os.getenv("TASTY_AGENT_MCP_URL", "http://localhost:8000/mcp")
MODAL_KEY = os.getenv("MODAL_KEY")
MODAL_SECRET = os.getenv("MODAL_SECRET")


async def main():
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {}
    if MODAL_KEY and MODAL_SECRET:
        headers["Modal-Key"] = MODAL_KEY
        headers["Modal-Secret"] = MODAL_SECRET

    async with (
        streamablehttp_client(MCP_URL, headers=headers) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()

        tool_name = sys.argv[1] if len(sys.argv) > 1 else None
        tool_args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}

        if not tool_name:
            tools = await session.list_tools()
            print(f"{len(tools.tools)} tools available:\n")
            for t in tools.tools:
                print(f"  {t.name}: {t.description[:80]}")
            return

        result = await session.call_tool(tool_name, tool_args)
        for item in result.content:
            print(item.text if hasattr(item, "text") else item)


if __name__ == "__main__":
    asyncio.run(main())
