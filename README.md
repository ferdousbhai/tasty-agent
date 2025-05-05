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

### Tools

#### Portfolio Management

1. `get_nlv_history`
   - Gets account net liquidating value (NLV) history.
   - Input:
     - `time_back` (string): Time period ('1d', '1m', '3m', '6m', '1y', 'all', default '1y').
   - Returns: Formatted table with Date and Value columns.

2. `get_account_balances`
   - Get current account balances.
   - Returns: Formatted string with cash balance, derivative buying power, net liquidating value, and maintenance excess.

3. `get_open_positions`
   - Get all currently open positions.
   - Returns: Formatted table showing Symbol, Type, Quantity, Mark Price, and Value.

4. `get_transaction_history`
   - Get transaction history.
   - Input:
     - `start_date` (string, optional): Start date in YYYY-MM-DD format. Defaults to last 90 days.
   - Returns: Formatted table showing Date, Sub Type, Description, and Value.

#### Trade Management

1. `schedule_trade`
   - Schedules a stock/option trade for immediate or next-market-open execution. Uses a lock for sequential processing.
   - Inputs:
     - `action` (string): "Buy to Open" or "Sell to Close".
     - `quantity` (integer): Number of shares/contracts.
     - `underlying_symbol` (string): The underlying stock symbol.
     - `strike` (float, optional): Option strike price.
     - `option_type` (string, optional): "C" for calls, "P" for puts.
     - `expiration_date` (string, optional): Option expiration date in YYYY-MM-DD format.
     - `dry_run` (boolean): Simulate without executing (default: False).
   - Returns: Message indicating immediate execution result (success/failure) or confirmation that the trade is scheduled (with Job ID).

2. `list_scheduled_trades`
   - List trades currently scheduled (waiting for market open/lock) or actively processing.
   - Returns: Formatted string listing Job ID and description for each relevant trade.

3. `cancel_scheduled_trade`
   - Cancel a trade previously scheduled for future execution (status must be 'scheduled').
   - Input:
     - `job_id` (string): ID of the scheduled job to cancel.
   - Returns: Confirmation or error message.

#### Market Analysis

1. `get_metrics`
   - Get market metrics for specified symbols.
   - Input:
     - `symbols` (list[string]): List of stock symbols.
   - Returns: Formatted table showing IV Rank, IV Percentile, Beta, Liquidity Rating, Lendability, and Earnings info (when available).

2. `get_prices`
   - Get current bid/ask prices for a stock or a specific option contract.
   - Input:
     - `underlying_symbol` (string): Stock ticker symbol.
     - `expiration_date` (string, optional): Option expiry in YYYY-MM-DD format.
     - `option_type` (string, optional): "C" for Call, "P" for Put.
     - `strike` (float, optional): Option strike price.
   - Returns: Formatted string with current bid and ask prices, or an error message.

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
