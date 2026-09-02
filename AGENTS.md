# tasty-agent

MCP server for Tastytrade account data, market data, watchlists, and order workflows.

## Code index

- `tasty_agent/server.py` — FastMCP tools, transport entry point, rate limiting, and orchestration
- `tasty_agent/orders.py` — instrument resolution, leg construction, quote-derived pricing, tick rounding, and budget sizing
- `tasty_agent/core.py` — session and account lifecycle
- `tasty_agent/market_data.py` — DXLink quotes and Greeks
- `tasty_agent/account_helpers.py` — compact account, balance, position, order, and transaction output
- `tasty_agent/watchlists.py` — watchlist operations
- `tests/` — unit and credential-gated integration tests
- `examples/` — local clients and deployment examples
- `commands/portfolio.md` — Claude Code command surface
- `skills/trading/SKILL.md` — Claude Code trading skill

## Boundaries

- All SDK calls share the existing two-requests-per-second limiter.
- Option chains use the existing 24-hour cache; tests that depend on chain changes must invalidate it explicitly.
- Order pricing must use the helpers in `orders.py`; preserve signed debit/credit semantics and broker dry-run safety.
- Keep MCP output compact and never replace selected projections with full SDK payloads.

## Commands

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

Credential-gated tests and live brokerage calls require explicit authorization.
