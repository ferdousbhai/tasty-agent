# tasty-agent: A TastyTrade MCP Server

A Model Context Protocol server for TastyTrade brokerage accounts. Enables LLMs to monitor portfolios, analyze positions, and execute trades with comprehensive market data and order management capabilities.

## Installation

```bash
uvx tasty-agent
```

### Authentication

Set up credentials via command line (stored in system keyring):

```bash
uvx tasty-agent setup
```

Or set environment variables:
- `TASTYTRADE_USERNAME`
- `TASTYTRADE_PASSWORD`
- `TASTYTRADE_ACCOUNT_ID` (optional - uses first account if multiple)

## MCP Tools

### Account Information
- **`get_account_balances`** - Get current cash balance, buying power, net liquidating value, and maintenance excess
  - Always returns fresh data from TastyTrade API
  - No parameters required

- **`get_current_positions`** - Get all open stock and option positions
  - Includes symbol, type, quantity, mark price, and current value
  - Always returns fresh data with current mark prices
  - No parameters required

- **`get_live_orders`** - Get all currently live (open) orders
  - Shows order ID, symbol, action, quantity, type, price, and status
  - Always returns fresh data from TastyTrade API
  - No parameters required

### Trading Operations
- **`place_trade`** - Execute stock/option trades
  - Supports: Buy to Open, Sell to Close
  - Auto-calculates mid-price or accepts custom limit price
  - Market hours validation (preventive for live trades)
  - Dry-run testing capability
  - Parameters: action, quantity, underlying_symbol, strike_price*, option_type*, expiration_date*, order_price*, dry_run

- **`cancel_order`** - Cancel live orders by ID
  - Dry-run testing supported
  - Parameters: order_id, dry_run

- **`modify_order`** - Modify existing order quantity or price
  - Single-leg orders only
  - Validates order is editable before modification
  - Parameters: order_id, new_quantity*, new_price*, dry_run

### Portfolio Analysis
- **`get_nlv_history`** - Net Liquidating Value history with OHLC data
  - Time periods: 1d, 1m, 3m, 6m, 1y, all
  - Returns formatted table with dates sorted most recent first
  - Parameters: time_back (default: 1y)

- **`get_transaction_history`** - Detailed transaction history
  - Defaults to last 90 days if no start date provided
  - Includes date, sub-type, description, and net value
  - Parameters: start_date* (YYYY-MM-DD format)

### Market Data & Analysis
- **`get_metrics`** - Comprehensive market metrics for symbols
  - IV Rank, IV Percentile, Beta, Liquidity Rating, Lendability, Earnings dates
  - Supports multiple symbols in single request
  - Parameters: symbols (list of strings)

- **`get_prices`** - Real-time bid/ask quotes via DXLink streaming
  - Supports both stocks and options
  - May return stale data when market is closed
  - Parameters: underlying_symbol, expiration_date*, option_type*, strike_price*

- **`check_market_status`** - Current market status with next open time
  - Uses NYSE calendar for accurate trading hours
  - Shows time remaining until next market open when closed
  - No parameters required

### Utilities
- **`wait`** - Wait for a specified duration in seconds
  - Duration limited between 1-60 seconds per call
  - Use multiple calls for longer waits
  - Parameters: duration_seconds (default: 60)

*\* Optional parameters*

## Key Features

### Performance & Efficiency
- **Fresh Data**: Account information always fetched live from TastyTrade API
- **Efficient Streaming**: Real-time quotes via DXLink WebSocket
- **Optimized Operations**: Direct API calls minimize latency

### Safety & Validation
- **Market Hours Protection**: Prevents live trades when market is closed
- **Price Validation**: Automatic bid-ask range checking and adjustment
- **Dry-Run Testing**: Test all operations without executing
- **Order Status Validation**: Confirms orders are modifiable before changes

### Intelligence & Automation
- **Auto-Pricing**: Mid-price calculation when no price specified
- **Price Adjustment**: User prices adjusted to valid bid-ask range when necessary
- **Instrument Resolution**: Automatic option chain lookup and instrument creation
- **Error Handling**: Comprehensive error messages and warnings

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

### Basic Trading
```
"Buy 100 shares of AAPL at market price"
"Sell 50 shares of TSLA at $250"
"Test buying 5 NVDA call options, strike 500, expiring 2024-12-20" (dry run)
```

### Order Management
```
"Get my live orders"
"Cancel order 12345"
"Change order 67890 quantity to 200 shares"
"Modify order 11111 price to $150"
```

### Portfolio Analysis
```
"Get my current positions"
"Get my account balance and buying power"
"Get my portfolio performance over the last 6 months"
"Show transaction history from 2024-01-01"
```

### Market Research
```
"Get current prices for AAPL December 200 calls"
"Check metrics for TSLA, NVDA, AAPL"
"Is the market currently open?"
"Get IV rank and earnings data for SPY, QQQ, IWM"
```

## Development

Debug with MCP inspector:
```bash
npx @modelcontextprotocol/inspector uvx tasty-agent
```

## License

MIT License
