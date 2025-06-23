# tasty-agent: A TastyTrade MCP Server

A Model Context Protocol server for TastyTrade brokerage accounts. Enables LLMs to monitor portfolios, analyze positions, and execute trades.

## Installation

```bash
uvx tasty-agent
```

### Authentication

Set up credentials (stored in system keyring):
```bash
uvx tasty-agent setup
```

Or use environment variables:
- `TASTYTRADE_USERNAME`
- `TASTYTRADE_PASSWORD`
- `TASTYTRADE_ACCOUNT_ID` (optional)

## MCP Tools

### Account & Portfolio
- **`get_balances()`** - Account balances and buying power
- **`get_positions()`** - All open positions with current values
- **`get_net_liquidating_value_history(time_back='1y')`** - Portfolio value history ('1d', '1m', '3m', '6m', '1y', 'all')
- **`get_history(start_date=None)`** - Transaction history (format: YYYY-MM-DD, default: last 90 days)

### Market Data & Research
- **`get_option_streamer_symbols(underlying_symbol, expiration_date, min_strike_price, max_strike_price, option_type)`** - Get option chain streamer symbols (option_type: 'C' or 'P', expiration_date: YYYY-MM-DD)
- **`get_quote(streamer_symbols, timeout=10.0)`** - Real-time quotes via DXLink streaming
- **`get_market_metrics(symbols)`** - IV rank, percentile, beta, liquidity for multiple symbols
- **`market_status(exchanges=['Equity'])`** - Market hours and status ('Equity', 'CME', 'CFE', 'Smalls')
- **`search_symbols(symbol)`** - Search for symbols by name/ticker

### Order Management
- **`get_live_orders()`** - Currently active orders
- **`place_order(legs, order_type='Limit', time_in_force='Day', price=None, dry_run=False)`** - Place orders
- **`delete_order(order_id)`** - Cancel orders by ID

### Watchlist Management
- **`get_public_watchlist_names()`** - Get available public watchlist names
- **`get_public_watchlist_entries(name)`** - Get entries from a public watchlist
- **`get_private_watchlists(name=None)`** - Get private watchlists (all if name=None, specific if name provided)
- **`create_private_watchlist(name, entries=[], group_name='main')`** - Create new private watchlist
- **`add_symbol_to_private_watchlist(watchlist_name, symbol, instrument_type)`** - Add symbol to existing watchlist
- **`remove_symbol_from_private_watchlist(watchlist_name, symbol, instrument_type)`** - Remove symbol from watchlist
- **`delete_private_watchlist(name)`** - Delete private watchlist

## Order Format

Orders use legs formatted as follows:
```json
[
  {
    "symbol": "AAPL",
    "quantity": "100",
    "action": "Buy",
    "instrument_type": "Equity"
  }
]
```

**Actions**:
- Equity: `Buy`, `Sell`
- Options: `Buy to Open`, `Sell to Open`, `Buy to Close`, `Sell to Close`
- Futures: `Buy`, `Sell`

**Instrument Types**: `Equity`, `Equity Option`, `Future`, `Future Option`, `Cryptocurrency`, `Warrant`


## Watchlist Entry Format

Watchlist entries use this format:
```json
[
  {
    "symbol": "AAPL",
    "instrument_type": "Equity"
  },
  {
    "symbol": "AAPL240119C00150000",
    "instrument_type": "Equity Option"
  }
]
```

## Key Features

- **Multi-leg strategies** with complex option spreads
- **Real-time streaming** quotes via DXLink WebSocket
- **Watchlist management** for portfolio organization
- **Dry-run testing** for all order operations
- **Automatic symbol normalization** for options
- **Fresh data** always from TastyTrade API

## Usage with Claude Desktop

Add to `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "tastytrade": {
      "command": "uvx",
      "args": ["tasty-agent"]
    }
  }
}
```

## Examples

```
"Get my account balances and current positions"
"Show AAPL option streamer symbols for next Friday expiration"
"Get real-time quote for SPY"
"Place dry-run order: buy 100 AAPL shares at market"
"Cancel order 12345"
"Create a watchlist called 'Tech Stocks' with AAPL and MSFT"
"Add TSLA to my Tech Stocks watchlist"
```

## Development

Debug with MCP inspector:
```bash
npx @modelcontextprotocol/inspector uvx tasty-agent
```

## License

MIT License
