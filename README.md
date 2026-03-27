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
- **`account_overview(include=["balances","positions"], time_back='1y')`** - Account balances, open positions, and/or portfolio value history in a single call. Set `include` to any combination of `"balances"`, `"positions"`, `"net_liq_history"`.

### Market Data & Research
- **`get_quotes(instruments, timeout=10.0)`** - Real-time quotes for stocks, options, futures, and indices via DXLink streaming
- **`get_greeks(options, timeout=10.0)`** - Greeks (delta, gamma, theta, vega, rho) for options via DXLink streaming
- **`get_market_metrics(symbols)`** - IV rank, percentile, beta, liquidity for multiple symbols
- **`market_status(exchanges=['Equity'])`** - Market hours, status, holidays, and current NYC time ('Equity', 'CME', 'CFE', 'Smalls')
- **`search_symbols(symbol)`** - Search for symbols by name/ticker

### History
- **`get_history(type, days=None, underlying_symbol=None, transaction_type=None)`** - Transaction history (`type="transactions"`, default 90 days) or order history (`type="orders"`, default 7 days). Filter transactions by `"Trade"` or `"Money Movement"`.

### Order Management
- **`manage_order(action, ...)`** - Unified order management:
  - `action="list"` - Get all live orders
  - `action="place"` - Place multi-leg orders with automatic mid-price discovery. Supports `Day`, `GTC`, `GTD`, `Ext`, `Ext Overnight`, `GTC Ext`, `GTC Ext Overnight`, and `IOC` time-in-force.
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

## Key Features

### Smart Order Placement
- Automatic price calculation from real-time market quotes when no price specified
- Multi-leg options strategies (spreads, strangles, etc.) with single function call
- Dry-run mode for testing orders without execution

### Rate Limiting & Reliability
- Built-in rate limiting (2 requests/second) prevents API throttling
- Comprehensive error handling and logging

### MCP Client Configuration

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

## Examples

```
"Get my account balances and current positions"
"Get my portfolio value history for the last 3 months"
"Get real-time quotes for SPY and AAPL"
"Get quotes for TQQQ C option with strike 100 expiring 2026-01-16"
"Get Greeks for AAPL P option with strike 150 expiring 2024-12-20"
"Buy 100 AAPL shares" (auto-pricing)
"Buy 100 AAPL at $150"
"Buy to open 17 TQQQ calls, strike 100, exp 2026-01-16"
"Place a call spread: buy to open AAPL 150C and sell to open AAPL 155C, both exp 2024-12-20"
"Close my AAPL position: sell to close 10 AAPL calls"
"Modify order 12345 to price $10.05"
"Cancel order 12345"
"Show my live orders"
"Get my trading history from January"
"Get my order history for SPY"
"Get my private watchlists"
"Add TSLA and NVDA to my tech watchlist"
"Remove AAPL from my tech watchlist"
```

## Remote Deployment (Modal)

Deploy as a remote MCP server on [Modal](https://modal.com) with proxy auth:

```bash
pip install modal && modal setup

# Create secrets
modal secret create tasty-agent-secrets \
  TASTYTRADE_CLIENT_SECRET=your_secret \
  TASTYTRADE_REFRESH_TOKEN=your_token

# Create a proxy auth token at https://modal.com/settings/proxy-auth-tokens

# Update MODAL_HOST in examples/modal_deploy.py with your workspace name

# Deploy
modal deploy examples/modal_deploy.py
```

Clients authenticate with `Modal-Key` and `Modal-Secret` headers. See [`examples/modal_deploy.py`](examples/modal_deploy.py) for the full setup.

## Development

### Testing with chat.py

For interactive testing during development:
```bash
# Set up environment variables in .env file:
# TASTYTRADE_CLIENT_SECRET=your_secret
# TASTYTRADE_REFRESH_TOKEN=your_token
# TASTYTRADE_ACCOUNT_ID=your_account_id (defaults to the first account)
# OPENAI_API_KEY=your_openai_key (you can provide alternative provider of your choice as supported by pydantic-ai)
# MODEL_IDENTIFIER=model_provider:model_name (defaults to openai:gpt-5-mini)


# Run the interactive client
uv run chat.py
```

The client provides a chat interface to test MCP tools directly. Example commands:
- "Get my account balances"
- "Get quote for SPY"
- "Place dry-run order: buy 100 AAPL at $150"

### Debug with MCP inspector

```bash
npx @modelcontextprotocol/inspector uvx tasty-agent
```

## License

MIT
