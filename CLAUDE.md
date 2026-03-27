# tasty-agent

TastyTrade MCP server (Python/FastMCP). Exposes 9 tools for portfolio management, trading, and market data via Model Context Protocol.

## Architecture

- **Entry**: `tasty_agent/server.py` — single file with all tools, helpers, and models
- **Auth**: OAuth2 refresh token → session token (auto-refreshes via tastytrade SDK)
- **Streaming**: DXLink WebSocket for real-time quotes and Greeks (with Trade event fallback for index symbols)
- **Rate limiting**: 2 req/s via `aiolimiter`
- **Caching**: Option chains cached 24h via `aiocache`
- **Transport**: stdio (default), sse, or streamable-http

## Tools (9)

| Tool | Purpose |
|------|---------|
| `account_overview` | Balances, positions, net liq history (concurrent fetch) |
| `get_history` | Transaction or order history with pagination |
| `manage_order` | Place/replace/cancel/list orders (auto-pricing from DXLink quotes) |
| `get_quotes` | Live streaming quotes (stocks, options, futures, indices) |
| `get_greeks` | Streaming Greeks for options |
| `get_market_metrics` | IV rank, beta, liquidity, earnings |
| `market_status` | Exchange hours, holidays, NYC time |
| `search_symbols` | Symbol search |
| `watchlist` | List/add/remove/delete watchlists |

## Commands

```bash
uv run tasty-agent              # stdio transport
uv run tasty-agent sse          # SSE transport
uv run tasty-agent streamable-http  # HTTP transport
uv run chat.py                  # Interactive test client
uv run pytest                   # Tests
npx @modelcontextprotocol/inspector uvx tasty-agent  # MCP inspector
```

## Environment

Requires `.env` or environment variables:
- `TASTYTRADE_CLIENT_SECRET` (required)
- `TASTYTRADE_REFRESH_TOKEN` (required)
- `TASTYTRADE_ACCOUNT_ID` (optional, defaults to first account)
