# tasty-agent: A TastyTrade MCP Server

A Model Context Protocol server for TastyTrade brokerage accounts. Enables LLMs to monitor portfolios, analyze positions, and execute trades. Includes rate limiting (5 req/sec) to prevent API errors.

## Authentication

**OAuth Setup**:
1. Create an OAuth app at https://my.tastytrade.com/app.html#/manage/api-access/oauth-applications
2. Check all scopes, save your client ID and client secret  
3. Create a "New Personal OAuth Grant" in your OAuth app settings (check all scopes)
4. Copy the generated refresh token
5. Configure the MCP server with your credentials (see Usage section below)

## MCP Tools

### Account & Portfolio
- **`get_balances()`** - Account balances and buying power
- **`get_positions()`** - All open positions with current values
- **`get_net_liquidating_value_history(time_back='1y')`** - Portfolio value history ('1d', '1m', '3m', '6m', '1y', 'all')
- **`get_history(start_date=None)`** - Transaction history (format: YYYY-MM-DD, default: last 90 days)

### Market Data & Research
- **`get_quote(symbol, option_type=None, strike_price=None, expiration_date=None, timeout=10.0)`** - Real-time quotes for stocks and options via DXLink streaming
- **`get_greeks(symbol, option_type, strike_price, expiration_date, timeout=10.0)`** - Greeks (delta, gamma, theta, vega, rho) for options via DXLink streaming
- **`get_market_metrics(symbols)`** - IV rank, percentile, beta, liquidity for multiple symbols
- **`market_status(exchanges=['Equity'])`** - Market hours and status ('Equity', 'CME', 'CFE', 'Smalls')
- **`search_symbols(symbol)`** - Search for symbols by name/ticker
- **`get_current_time_nyc()`** - Current time in New York timezone (market time)

### Order Management
- **`get_live_orders()`** - Currently active orders
- **`place_order(symbol, order_type, action, quantity, price=None, strike_price=None, expiration_date=None, time_in_force='Day', dry_run=False, override_price_protection=False)`** - Smart order placement with automatic price discovery and protection
  - Auto-pricing: Uses mid-price between bid/ask (rounded to nearest 5¢) when price=None
  - Price protection: Prevents buying above ask or selling below bid (unless overridden)
- **`delete_order(order_id)`** - Cancel orders by ID

### Watchlist Management
- **`get_watchlists(watchlist_type='private', name=None)`** - Get watchlists ('public'/'private', all if name=None)
- **`manage_private_watchlist(action, symbol, instrument_type, name='main')`** - Add/remove symbols from private watchlists
- **`delete_private_watchlist(name)`** - Delete private watchlist

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
"Get real-time quote for SPY"
"Get quote for TQQQ C option with strike 100 expiring 2026-01-16"
"Get Greeks (delta, gamma, theta, vega, rho) for AAPL P option with strike 150 expiring 2024-12-20"
"Buy 100 AAPL shares" (auto-pricing)
"Buy 100 AAPL at $150" (with protection)
"Buy 17 TQQQ calls, strike 100, exp 2026-01-16"
"Cancel order 12345"
"Get my private watchlists"
"Add TSLA to my main watchlist"
"Remove AAPL from my tech watchlist"
```

## Background Trading Bot

Run automated trading strategies with `background.py`:

```bash
# Run once with instructions
uv run background.py "Check my portfolio and rebalance if needed"

# Run every hour
uv run background.py "Monitor SPY and alert on significant moves" --hourly

# Run every day
uv run background.py "Generate daily portfolio summary" --daily

# Custom period (seconds)
uv run background.py "Scan for covered call opportunities" --period 1800  # every 30 minutes

# Schedule start time (NYC timezone)
uv run background.py "Execute morning trading strategy" --schedule "9:30am" --hourly

# Market open shorthand (9:30am)
uv run background.py "Buy the dip strategy" --market-open --hourly
```

## Development

### Testing with client.py

For interactive testing during development:
```bash
# Install dev dependencies
uv sync --group dev

# Set up environment variables in .env file:
# TASTYTRADE_CLIENT_SECRET=your_secret
# TASTYTRADE_REFRESH_TOKEN=your_token  
# TASTYTRADE_ACCOUNT_ID=your_account_id (optional)
# OPENAI_API_KEY=your_openai_key

# Run the interactive client
uv run client.py
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
