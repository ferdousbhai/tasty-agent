# tasty-agent: A TastyTrade MCP Server

## Overview

A Model Context Protocol server for interacting with TastyTrade brokerage accounts. This server enables Large Language Models to monitor portfolios, analyze positions, and execute trades through the TastyTrade platform.

Please note that tasty-agent is currently in early development. The functionality and available tools are subject to change and expansion as development continues.

## Prerequisites

- Python 3.12 or higher
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
tasty-setup
```

### Tools

#### Portfolio Management

1. `plot_nlv_history`
   - Plots account net liquidating value history over time
   - Input:
     - `time_back` (string): Time period to plot ('1d', '1m', '3m', '6m', '1y', 'all')
   - Returns: Displays a matplotlib plot of portfolio value history
   - Example response: `"Generated plot showing NLV trend from $10,000 to $12,500 over the last 3 months"`

2. `get_account_balances`
   - Get current account balances
   - Returns: Formatted string with cash balance, buying power, and net liquidating value
   - Example response: `"Cash: $5,000.00, Buying Power: $10,000.00, NLV: $15,000.00"`

3. `get_open_positions`
   - Get all currently open positions
   - Returns: Formatted string showing all open positions
   - Example response: `"AAPL: 100 shares @ $150.00, TSLA 300P 2024-06-21: -2 contracts @ $5.00"`

#### Order Management

1. `queue_order_tool`
   - Queue a new order for later execution
   - Inputs:
     - `symbol` (string): Trading symbol (e.g., "AAPL" or "INTC 50C 2026-01-16")
     - `quantity` (integer): Number of shares/contracts
     - `action` (string): "Buy to Open" or "Sell to Close"
     - `execution_group` (integer, optional): Group number for batch execution
     - `dry_run` (boolean, optional): Test order without execution
   - Returns: Order confirmation message

2. `review_queue_tool`
   - Review all currently queued orders
   - Returns: Formatted string showing all queued orders

3. `execute_orders_tool`
   - Execute all queued orders
   - Input:
     - `force` (boolean, optional): Execute even when market is closed
   - Returns: Execution status message

4. `cancel_orders_tool`
   - Cancel queued orders based on filters
   - Inputs:
     - `execution_group` (integer, optional): Group number to cancel
     - `symbol` (string, optional): Symbol to cancel
   - Returns: Cancellation confirmation message

#### Market Analysis

1. `get_metrics`
   - Get market metrics for specified symbols
   - Input:
     - `symbols` (string[]): List of stock symbols
   - Returns: Formatted string showing IV rank, liquidity, beta, etc.

2. `get_prices`
   - Get current bid and ask prices
   - Input:
     - `symbol` (string): Stock or option symbol
   - Returns: Formatted string showing bid and ask prices

3. `get_transaction_history`
   - Get transaction history
   - Input:
     - `start_date` (string, optional): Start date in YYYY-MM-DD format
   - Returns: Formatted string showing transaction history

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

## Debugging

You can use the MCP inspector to debug the server:

```bash
npx @modelcontextprotocol/inspector uvx tasty-agent
```

For logs, run:

```bash
tail -n 20 -f ~/Library/Logs/Claude/mcp*.log
```

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
        "path/to/tasty-agent",
        "run",
        "tasty-agent"
      ]
    }
  }
}
```

## Security Notice

This server handles sensitive financial information and can execute trades. Always:

- Use secure credential storage
- Review queued orders before execution
- Use dry-run mode for testing

## License

This MCP server is licensed under the MIT License. See the LICENSE file for details.
