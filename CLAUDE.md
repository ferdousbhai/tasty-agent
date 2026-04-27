# tasty-agent

TastyTrade MCP server (Python/FastMCP). Exposes 13 tools for portfolio management, trading, and market data via Model Context Protocol.

## Architecture

- **Entry**: `tasty_agent/server.py` — MCP tools and orchestration
- **Orders**: `tasty_agent/orders.py` — instrument resolution, leg building, guarded quote-derived pricing, and budget sizing
- **Auth**: OAuth2 refresh token → session token (auto-refreshes via tastytrade SDK)
- **Streaming**: DXLink WebSocket for real-time quotes and Greeks (with Trade event fallback for index symbols)
- **Rate limiting**: 2 req/s via `aiolimiter`
- **Caching**: Option chains cached 24h via `aiocache`
- **Output shape**: Tools return compact tables/dicts with selected actionable fields instead of full SDK dumps
- **Transport**: stdio (default), sse, or streamable-http

## Tools (13)

| Tool | Purpose |
|------|---------|
| `account_overview` | Balances (incl. net liq) and positions (concurrent fetch) |
| `get_history` | Transaction or order history with pagination |
| `place_order` | Place quote-derived mid orders with optional dollar sizing and chase |
| `replace_order` | Reprice an existing live order at current quote-derived mid |
| `cancel_order` | Cancel a live order by id |
| `list_orders` | List live orders |
| `get_quotes` | Live streaming quotes (stocks, options, futures, indices) |
| `get_greeks` | Streaming Greeks for options |
| `get_gex` | Gamma exposure analysis (net GEX, flip level, call/put walls) |
| `get_market_metrics` | IV rank, beta, liquidity, earnings |
| `market_status` | Exchange hours, holidays, NYC time |
| `search_symbols` | Symbol search |
| `watchlist` | List/add/remove/delete watchlists |

## Commands

```bash
uv run tasty-agent                # stdio transport
uv run tasty-agent sse            # SSE transport
uv run tasty-agent streamable-http  # HTTP transport
uv run pytest                     # Tests
uv run examples/chat.py           # Interactive test client
uv run examples/mcp_client.py     # Remote MCP client
npx @modelcontextprotocol/inspector uvx tasty-agent  # MCP inspector
```

## Environment

Requires `.env` or environment variables:
- `TASTYTRADE_CLIENT_SECRET` (required)
- `TASTYTRADE_REFRESH_TOKEN` (required)
- `TASTYTRADE_ACCOUNT_ID` (optional, defaults to first account)

## Examples

- `examples/modal_deploy.py` — Deploy to Modal with proxy auth
- `examples/mcp_client.py` — Python client for remote MCP server
- `examples/chat.py` — Interactive chat via pydantic-ai
- `examples/background.py` — Background trading bot with scheduling
