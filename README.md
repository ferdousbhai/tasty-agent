# tasty-agent: A TastyTrade MCP Server
[![Trust Score](https://archestra.ai/mcp-catalog/api/badge/quality/ferdousbhai/tasty-agent)](https://archestra.ai/mcp-catalog/ferdousbhai__tasty-agent)

A Model Context Protocol server for TastyTrade brokerage accounts. Enables LLMs to monitor portfolios, analyze positions, and execute trades. Features automated IV analysis prompts and built-in rate limiting (2 requests/second) to prevent API errors.

## Authentication

**OAuth Setup**:
1. Create an OAuth app at https://my.tastytrade.com/app.html#/manage/api-access/oauth-applications
2. Check all scopes, save your client ID and client secret
3. Create a "New Personal OAuth Grant" in your OAuth app settings (check all scopes)
4. Copy the generated refresh token
5. Configure the MCP server with your credentials (see Usage section below)

## MCP Tools

### Account & Portfolio
- **`account_overview(include=["balances","positions"])`** - Account balances (including net liquidating value) and open positions.

### Market Data & Research
- **`get_quotes(instruments, timeout=10.0)`** - Real-time quotes for stocks, options, futures, and indices via DXLink streaming
- **`get_greeks(options, timeout=10.0)`** - Greeks (delta, gamma, theta, vega, rho) for options via DXLink streaming
- **`get_gex(symbol, expiration_date, timeout=60.0)`** - Gamma Exposure analysis: net GEX, gamma regime, flip level, call/put walls, and top strikes
  - Intentionally returns compact analysis rather than raw full-chain per-strike data, to avoid oversized MCP responses on large option chains.
- **`get_market_metrics(symbols)`** - IV rank, percentile, beta, liquidity for multiple symbols
- **`market_status(exchanges=['Equity'])`** - Market hours, status, holidays, and current NYC time ('Equity', 'CME', 'CFE', 'Smalls')
- **`search_symbols(symbol, max_results=20)`** - Search for symbols by name/ticker

### History
- **`get_history(type, days=None, underlying_symbol=None, transaction_type=None, page_offset=0, max_results=50)`** - Transaction history (`type="transactions"`, default 90 days) or order history (`type="orders"`, default 7 days). Paginated — use `page_offset` and `max_results` for large result sets. Filter transactions by `"Trade"` or `"Money Movement"`.

### Order Management
- **`manage_order(action, ...)`** - Unified order management:
  - `action="list"` - Get all live orders
  - `action="place"` - Place multi-leg orders with automatic mid-price discovery. Supports `Day`, `GTC`, `GTD`, `Ext`, `Ext Overnight`, `GTC Ext`, `GTC Ext Overnight`, and `IOC` time-in-force.
  - Order actions follow the tastytrade Python SDK contract: equities and options use `Buy to Open`, `Buy to Close`, `Sell to Open`, or `Sell to Close`; futures use `Buy` or `Sell`.
  - `action="replace"` - Modify existing order price
  - `action="cancel"` - Cancel an order

### Watchlist Management
- **`watchlist(action, ...)`** - Unified watchlist management:
  - `action="list"` - Get public or private watchlists
  - `action="add"` - Add symbols to a watchlist (creates if doesn't exist)
  - `action="remove"` - Remove symbols from a watchlist
  - `action="delete"` - Delete a watchlist

### MCP Prompts
- **IV Rank Analysis** - Automated prompt to analyze IV rank extremes across positions and watchlists for entry/exit opportunities

## Usage

### Local (stdio)

Add to your MCP client configuration (e.g., `claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "tastytrade": {
      "command": "uvx",
      "args": ["tasty-agent"],
      "env": {
        "TASTYTRADE_CLIENT_SECRET": "your_client_secret",
        "TASTYTRADE_REFRESH_TOKEN": "your_refresh_token",
        "TASTYTRADE_ACCOUNT_ID": "your_account_id"
      }
    }
  }
}
```

### Remote (Modal)

Deploy as a remote MCP server on [Modal](https://modal.com) with proxy auth:

```bash
pip install modal && modal setup

# Create secrets
modal secret create tasty-agent-secrets \
  TASTYTRADE_CLIENT_SECRET=your_secret \
  TASTYTRADE_REFRESH_TOKEN=your_token

# Create a proxy auth token at https://modal.com/settings/proxy-auth-tokens

# Export your workspace host
export MODAL_HOST=<workspace>--tasty-agent-mcp-server.modal.run

# Deploy
modal deploy examples/modal_deploy.py
```

Clients authenticate with `Modal-Key` and `Modal-Secret` headers. See [`examples/modal_deploy.py`](examples/modal_deploy.py).

Tagged releases can also auto-deploy the Modal app. Configure these GitHub settings once:

- secrets: `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`
- variable: `MODAL_HOST`
- optional variable: `TASTY_AGENT_SECRET_NAME` if you do not use the default `tasty-agent-secrets`

After that, pushing a release tag like `v4.1.2` will publish to PyPI and deploy Modal with `tasty-agent==4.1.2`.

### Programmatic (Python client)

Connect to a remote tasty-agent and call tools directly:

```bash
# List all tools
uv run examples/mcp_client.py

# Call a tool
uv run examples/mcp_client.py market_status
uv run examples/mcp_client.py get_market_metrics '{"symbols": ["AAPL", "SPY"]}'
```

See [`examples/mcp_client.py`](examples/mcp_client.py) for the full client code.

## Examples

```
"Get my account balances and current positions"
"What's my net liquidating value?"
"Get real-time quotes for SPY and AAPL"
"Get quotes for TQQQ C option with strike 100 expiring 2026-01-16"
"Get Greeks for AAPL P option with strike 150 expiring 2024-12-20"
"Show GEX analysis for SPY options expiring 2026-04-03"
"Buy to open 100 AAPL shares" (auto-pricing)
"Buy to open 100 AAPL at $150"
"Buy to open 17 TQQQ calls, strike 100, exp 2026-01-16"
"Place a call spread: buy to open AAPL 150C and sell to open AAPL 155C, both exp 2024-12-20"
"Buy 1 /ESM26 future at 10.0 debit"
"Modify order 12345 to price $10.05"
"Cancel order 12345"
"Show my live orders"
"Get my trading history from January"
"Get my order history for SPY"
"Get my private watchlists"
"Add TSLA and NVDA to my tech watchlist"
```

## Development

```bash
# Run tests
uv run pytest

# Interactive chat client (requires .env with credentials + OPENAI_API_KEY)
uv run examples/chat.py

# Background trading bot
uv run examples/background.py "Check portfolio and rebalance" --hourly

# Debug with MCP inspector
npx @modelcontextprotocol/inspector uvx tasty-agent
```

## License

MIT
