# tasty-agent: A TastyTrade MCP Server

## Overview

A Model Context Protocol server for interacting with TastyTrade brokerage accounts. This server enables Large Language Models to monitor portfolios, analyze positions, and execute trades through the TastyTrade platform.

## Prerequisites

- Python 3.12
- [uv](https://docs.astral.sh/uv/) package manager
- A TastyTrade account

## Installation

Install uv if you haven't already:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

We will use `uvx` to directly run tasty-agent:

```bash
uvx tasty-agent
```

### Authentication

The server requires TastyTrade credentials. For security, this is set up via command line and stored in your system's keyring (Keychain on macOS, Windows Credential Manager on Windows, or similar secure storage on other platforms):

```bash
uvx tasty-agent setup
```

Alternatively, you can set the following environment variables:

- `TASTYTRADE_USERNAME`: Your Tastytrade username
- `TASTYTRADE_PASSWORD`: Your Tastytrade password
- `TASTYTRADE_ACCOUNT_ID`: Your Tastytrade account number (optional if you only have one account)

If credentials are found in both the keyring and environment variables, the keyring values will take precedence.

## MCP Resources

The server exposes key account information as MCP resources that are cached and automatically updated:

### Account Data Resources

1. **`account://balances`** - Current account balances including:
   - Cash balance
   - Equity buying power
   - Derivative buying power
   - Net liquidating value
   - Maintenance excess

2. **`account://positions`** - All currently open positions showing:
   - Symbol, type, quantity
   - Current mark price and total value
   - Real-time position data

3. **`account://live-orders`** - All active orders with:
   - Order ID, symbol, action
   - Quantity, price, order type
   - Current status

These resources are automatically cached and refreshed every 30 seconds or when trades are executed, providing the LLM with instant access to current account information without requiring function calls.

## MCP Tools

### Trade Management

1. **`place_trade`** - Execute stock/option trades
   - **Parameters:**
     - `action`: "Buy to Open" or "Sell to Close"
     - `quantity`: Number of shares/contracts
     - `underlying_symbol`: Stock ticker symbol
     - `strike_price`: Option strike price (required for options)
     - `option_type`: "C" for calls, "P" for puts (required for options)
     - `expiration_date`: Option expiry in YYYY-MM-DD format (required for options)
     - `order_price`: Optional limit price (defaults to mid-price if not specified)
     - `dry_run`: Test without executing (default: False)
   - **Features:**
     - Automatic mid-price calculation when no price specified
     - Price validation against bid-ask spread
     - Market hours validation (prevents live trades when market closed)
     - Supports both stocks and options

2. **`cancel_order`** - Cancel a live order by ID
   - **Parameters:**
     - `order_id`: The ID of the order to cancel
     - `dry_run`: Test without executing (default: False)
   - **Features:**
     - Validates order exists and is cancellable
     - Updates account cache after successful cancellation

3. **`modify_order`** - Modify a live order's quantity or price
   - **Parameters:**
     - `order_id`: The ID of the order to modify
     - `new_quantity`: New quantity for the order (optional)
     - `new_price`: New limit price for the order (optional)
     - `dry_run`: Test without executing (default: False)
   - **Features:**
     - At least one of new_quantity or new_price must be provided
     - Validates order is modifiable
     - Only supports single-leg orders
     - Updates account cache after successful modification

### Portfolio Analysis

1. **`get_nlv_history`** - Account net liquidating value history
   - **Parameters:**
     - `time_back`: Time period ('1d', '1m', '3m', '6m', '1y', 'all') - default: '1y'
   - **Returns:** Formatted table with Date, Open, High, Low, Close columns
   - **Features:** Data sorted by date (most recent first)

2. **`get_transaction_history`** - Account transaction history
   - **Parameters:**
     - `start_date`: Start date in YYYY-MM-DD format (optional, defaults to last 90 days)
   - **Returns:** Formatted table with Date, Sub Type, Description, Value columns
   - **Features:** Comprehensive transaction details including fees and adjustments

### Market Data & Information

1. **`get_metrics`** - Market metrics for symbols
   - **Parameters:**
     - `symbols`: List of stock symbols
   - **Returns:** Table with IV Rank, IV Percentile, Beta, Liquidity, Lendability, Earnings data
   - **Features:**
     - Implied volatility rankings and percentiles
     - Liquidity ratings and stock lendability
     - Upcoming earnings information with time of day

2. **`get_prices`** - Current bid/ask prices for stocks or options
   - **Parameters:**
     - `underlying_symbol`: Stock ticker symbol
     - `expiration_date`: Option expiry in YYYY-MM-DD format (for options)
     - `option_type`: "C" for calls, "P" for puts (for options)
     - `strike_price`: Option strike price (for options)
   - **Returns:** Current bid and ask prices
   - **Features:**
     - Real-time streaming quotes via DXLink
     - Supports both stocks and options
     - Note: May return stale data when market is closed

### Market Status

1. **`check_market_status`** - Check if market is currently open or closed
   - **Parameters:** None
   - **Returns:** Current market status and next open time if closed
   - **Features:**
     - Uses NYSE calendar for accurate market hours
     - Shows time remaining until next market open
     - Provides comprehensive market timing information

## Architecture Benefits

- **Efficient**: Account data available as cached resources (no repeated API calls)
- **Real-time**: Resources automatically updated after trades and every 30 seconds
- **Intelligent**: Automatic price discovery using mid-price when no price specified
- **Safe**: Market hours validation prevents accidental after-hours trading
- **Transparent**: Clear error messages and validation feedback
- **Fast**: Direct access to account data without function calls
- **Comprehensive**: Full trading lifecycle from analysis to execution to monitoring

## Usage with Claude Desktop

Add this to your `claude_desktop_config.json`:

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

## Example Workflows

### Basic Stock Trading
```
"Buy 100 shares of AAPL at market price"
→ Uses place_trade with automatic mid-price calculation

"Check my current positions"
→ Uses account://positions resource

"Cancel order 12345"
→ Uses cancel_order tool
```

### Options Trading
```
"Buy 5 TSLA call options, strike 250, expiring 2024-12-20"
→ Uses place_trade with option parameters

"Get current price for TSLA Dec 20 250 calls"
→ Uses get_prices with option parameters
```

### Portfolio Analysis
```
"Show my account performance over the last 6 months"
→ Uses get_nlv_history with time_back='6m'

"What are my transactions from the beginning of this year?"
→ Uses get_transaction_history with start_date='2024-01-01'
```

## Debugging

You can use the MCP inspector to debug the server:

```bash
npx @modelcontextprotocol/inspector uvx tasty-agent
```

For logs, check:

- macOS: `~/Library/Logs/Claude/mcp*.log`
- Windows: `%APPDATA%\Claude\logs\mcp*.log`

## Development

For local development testing:

1. Use the MCP inspector (see [Debugging](#debugging))
2. Test using Claude Desktop with this configuration:

```json
{
  "mcpServers": {
    "tastytrade": {
      "command": "uv",
      "args": [
        "--directory",
        "/path/to/tasty-agent",
        "run",
        "tasty-agent"
      ]
    }
  }
}
```

## Security Notes

- Credentials are stored securely in system keyring
- All trades can be tested with `dry_run=True` before execution
- Market hours validation prevents accidental after-hours trading
- Order validation ensures only valid, executable orders are placed

## License

This MCP server is licensed under the MIT License. See the LICENSE file for details.
