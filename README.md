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

The server requires TastyTrade credentials. For security, these are set up via command line and stored in your system's keyring (Keychain on macOS, Windows Credential Manager on Windows, or similar secure storage on other platforms):

```bash
uvx tasty-agent setup
```

### Tools

#### Portfolio Management

1. `plot_nlv_history`
   - Plots account net liquidating value history over time
   - Input:
     - `time_back` (string): Time period to plot ('1d', '1m', '3m', '6m', '1y', 'all')
   - Returns: Base64-encoded PNG image of the generated plot

2. `get_account_balances`
   - Get current account balances
   - Returns: Formatted string with cash balance, buying power, net liquidating value, and maintenance excess
   - Example response: `"Cash: $5,000.00, Buying Power: $10,000.00, NLV: $15,000.00"`

3. `get_open_positions`
   - Get all currently open positions
   - Returns: Formatted table showing Symbol, Position Type, Quantity, Mark Price, and Current Value

4. `get_transaction_history`
   - Get transaction history
   - Input:
     - `start_date` (string, optional): Start date in YYYY-MM-DD format. Defaults to last 90 days if not provided.
   - Returns: Formatted table showing Transaction Date, Transaction Type, Description, and Value

#### Trade Management

1. `schedule_trade`
   - Schedule a trade for execution
   - Inputs:
     - `action` (string): "Buy to Open" or "Sell to Close"
     - `quantity` (integer): Number of shares/contracts
     - `underlying_symbol` (string): The underlying stock symbol (e.g., "SPY", "AAPL")
     - `strike` (float, optional): For options only - strike price
     - `option_type` (string, optional): For options only - "C" for calls, "P" for puts
     - `expiration_date` (string, optional): For options only - expiration date in YYYY-MM-DD format
     - `dry_run` (boolean): Simulate without executing (default: False)
   - Returns: Task ID and confirmation message
   - Notes:
     - Trades execute immediately during market hours
     - Trades during market closure are automatically scheduled for next market open

2. `list_scheduled_trades`
   - List all scheduled trades
   - Returns: Formatted table showing Task ID, Time, Type, and Description

3. `remove_scheduled_trade`
   - Remove a scheduled trade
   - Input:
     - `task_id` (string): ID of task to remove
   - Returns: Confirmation message

#### Market Analysis

1. `get_metrics`
   - Get market metrics for specified symbols
   - Input:
     - `symbols` (string[]): List of stock symbols
   - Returns: Formatted table showing IV Rank, IV Percentile, Beta, Liquidity Rating, and Next Earnings Date/Time (when available)

2. `get_prices`
   - Get current bid and ask prices
   - Input:
     - `symbol` (string): Stock or option symbol
   - Returns: Current bid and ask prices

## Usage with Claude Desktop

Add this to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "tastytrade": {
      "command": "path/to/uvx/command/uvx",
      "args": ["tasty-agent"]
    }
  }
}
```

**Important**: Scheduled trades will only execute while Claude Desktop is running. When Claude Desktop is closed, the server shuts down and trades are not executed.

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
      "command": "path/to/uv/command/uv",
      "args": [
        "--directory",
        "path/to/tasty-agent",
        "run",
        "tasty-agent"
      ]
    }
  }
}
```

## License

This MCP server is licensed under the MIT License. See the LICENSE file for details.
