# tasty-agent

## Delivery

Prefer `main` — branches/PRs only if asked or for isolated experiments. For small/docs changes, lightweight checks are fine; otherwise simplify diff, fix blocking issues, run checks, then commit and push `origin/main`.

## Rules

- Respect the existing 2 req/s limiter (`aiolimiter`) — do not add parallel SDK calls without throttling.
- Option chains are cached 24h (`aiocache`) — invalidate explicitly when testing chain changes.
- Pricing is tick-rounded quote-derived mid — use `orders.py` helpers, do not hand-roll.
- Keep tool output shape compact (selected fields) — do not return full SDK dumps.

## Index

- [tasty_agent/server.py](tasty_agent/server.py) — MCP tools and orchestration
- [tasty_agent/orders.py](tasty_agent/orders.py) — instrument resolution, leg building, tick-rounded pricing, budget sizing
- [tasty_agent/core.py](tasty_agent/core.py) — client/session
- [tasty_agent/market_data.py](tasty_agent/market_data.py) — quotes/Greeks via DXLink
- [tasty_agent/account_helpers.py](tasty_agent/account_helpers.py) — account helpers
- [tasty_agent/watchlists.py](tasty_agent/watchlists.py) — watchlists
- [examples/chat.py](examples/chat.py) — interactive test client
- [examples/mcp_client.py](examples/mcp_client.py) — remote MCP client
- [tests/](tests/) — `uv run pytest`

## Commands

```bash
uv run tasty-agent                # stdio
uv run tasty-agent sse
uv run tasty-agent streamable-http
uv run pytest
```
