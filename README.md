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
- **`get_balances`** - Account balances and buying power
- **`get_positions`** - All open positions with current values
- **`get_live_orders`** - Currently active orders
- **`get_net_liquidating_value_history`** - Portfolio value history (1d, 1m, 3m, 6m, 1y, all)
- **`get_history`** - Transaction history (default: last 90 days)

### Order Management
- **`place_order`** - Place new orders (single/multi-leg strategies)
  - Parameters: `legs`, `order_type` (Limit/Market), `time_in_force` (Day/GTC/IOC), `price`, `dry_run`
- **`delete_order`** - Cancel orders by ID
- **`get_order`** - Get order details by ID
- **`replace_order`** - Modify existing orders

### Market Data
- **`get_option_chain`** - Option chain with filtering by expiration, strikes, type
- **`get_quote`** - Real-time quotes via DXLink streaming
- **`get_market_metrics`** - IV rank, percentile, beta, liquidity for multiple symbols
- **`check_market_status`** - Market hours and next open time

## Order Format

Orders use JSON leg format:
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

**Actions**: Equity: Buy/Sell | Options: Buy/Sell to Open/Close
**Option Symbols**: Auto-normalized from OCC to OSI format

## Key Features

- **Multi-leg strategies** with complex option spreads
- **Real-time streaming** quotes via DXLink WebSocket
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
"Show AAPL option chain for next Friday expiration"
"Get real-time quote for SPY"
"Place dry-run order: buy 100 AAPL shares at market"
"Cancel order 12345"
```

### Multi-leg Order Example
```python
# Iron condor strategy
legs = [
  {"symbol": "SPY240315P00480000", "quantity": "1", "action": "Sell to Open", "instrument_type": "Equity Option"},
  {"symbol": "SPY240315P00475000", "quantity": "1", "action": "Buy to Open", "instrument_type": "Equity Option"},
  {"symbol": "SPY240315C00520000", "quantity": "1", "action": "Sell to Open", "instrument_type": "Equity Option"},
  {"symbol": "SPY240315C00525000", "quantity": "1", "action": "Buy to Open", "instrument_type": "Equity Option"}
]
```

## Development

Debug with MCP inspector:
```bash
npx @modelcontextprotocol/inspector uvx tasty-agent
```

## License

MIT License
