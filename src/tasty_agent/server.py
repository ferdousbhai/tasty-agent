import asyncio
from typing import Literal
import logging
from uuid import uuid4
import sys
from datetime import timedelta, datetime, date

from tabulate import tabulate
from mcp.server.fastmcp import FastMCP
from tastytrade import metrics

from .auth_cli import auth
from .state import account_state
from .task import Task, scheduled_tasks
from .instrument import create_instrument
from .prices import get_prices as get_prices_internal

logger = logging.getLogger(__name__)

mcp = FastMCP("TastyTrade")

@mcp.tool()
async def schedule_trade(
    action: Literal["Buy to Open", "Sell to Close"],
    quantity: int,
    underlying_symbol: str,
    strike: float | None = None,
    option_type: Literal["C", "P"] | None = None,
    expiration_date: str | None = None,
    dry_run: bool = False,
) -> str:
    """Schedule a trade for execution.

    Args:
        action: Trade direction - "Buy to Open" to open a new position or "Sell to Close" to close existing position
        quantity: Number of contracts/shares to trade
        underlying_symbol: The underlying stock symbol (e.g., "SPY", "AAPL")
        strike: For options only - strike price
        option_type: For options only - "C" for calls, "P" for puts
        expiration_date: For options only - expiration date in YYYY-MM-DD format
        dry_run: If True, simulate the trade without actually placing it (default is False).

    Returns:
        String containing task ID and confirmation message
    """
    try:
        expiry_datetime = None
        if expiration_date:
            try:
                expiry_datetime = datetime.strptime(expiration_date, "%Y-%m-%d")
            except ValueError:
                return "Invalid expiration date format. Please use YYYY-MM-DD format"

        instrument = await create_instrument(
            underlying_symbol=underlying_symbol,
            expiration_date=expiry_datetime,
            option_type=option_type,
            strike=strike
        )
        if instrument is None:
            return f"Could not find instrument for symbol: {underlying_symbol}"

        task_id = str(uuid4())
        task = Task(
            task_id=task_id,
            instrument=instrument,
            quantity=quantity,
            action=action,
            dry_run=dry_run,
            description=f"{action} {quantity} {underlying_symbol}" + (
                f" {option_type}{strike} exp {expiration_date}" if option_type else ""
            )
        )
        scheduled_tasks[task_id] = task
        task._task = asyncio.create_task(task.execute())

        timing_info = task.get_execution_time_info()
        return f"Task {task_id} scheduled successfully - {timing_info}"

    except Exception as e:
        return f"Error scheduling trade: {str(e)}"

@mcp.tool()
async def list_scheduled_trades() -> str:
    """List all currently scheduled trades."""
    if not scheduled_tasks:
        return "No trades currently scheduled."

    output = ["Scheduled Tasks:", ""]
    output.append(f"{'Task ID':<36} {'Description':<40}")
    output.append("-" * 76)

    for task_id, task in scheduled_tasks.items():
        output.append(f"{task_id:<36} {task.description[:40]:<40}")

    return "\n".join(output)

@mcp.tool()
async def remove_scheduled_trade(task_id: str) -> str:
    """Remove a scheduled trade by its task ID."""
    if task_id not in scheduled_tasks:
        return f"Trade {task_id} not found."

    try:
        task = scheduled_tasks[task_id]
        if task._task and not task._task.done():
            task._task.cancel()
            try:
                await task._task
            except asyncio.CancelledError:
                pass # This is expected behavior (task has been cancelled).
        del scheduled_tasks[task_id]
        return f"Trade {task_id} cancelled successfully."
    except Exception as e:
        return f"Error removing trade {task_id}: {str(e)}"

@mcp.tool()
def plot_nlv_history(
    time_back: Literal['1d', '1m', '3m', '6m', '1y', 'all'] = '1y'
) -> str:
    """Plot the account's net liquidating value history and return as a base64 PNG image."""
    try:
        import io
        import base64
        import matplotlib
        import matplotlib.pyplot as plt

        history = account_state.account.get_net_liquidating_value_history(
            account_state.session,
            time_back=time_back
        )
        matplotlib.use("Agg")
        fig, ax = plt.subplots(figsize=(10, 6))
        ax.plot([n.time for n in history], [n.close for n in history], 'b-')
        ax.set_title(f'Portfolio Value History (Past {time_back})')
        ax.set_xlabel('Date')
        ax.set_ylabel('Portfolio Value ($)')
        ax.grid(True)

        buffer = io.BytesIO()
        fig.savefig(buffer, format='png')
        buffer.seek(0)
        base64_str = base64.b64encode(buffer.read()).decode('utf-8')
        plt.close(fig)
        return base64_str
    except Exception as e:
        return f"Error generating plot: {str(e)}"

@mcp.tool()
async def get_account_balances() -> str:
    """Get current account balances and buying power information."""
    try:
        balances = await account_state.get_balances()
        return (
            f"Account Balances:\n"
            f"Cash Balance: ${balances.cash_balance:,.2f}\n"
            f"Buying Power: ${balances.derivative_buying_power:,.2f}\n"
            f"Net Liquidating Value: ${balances.net_liquidating_value:,.2f}\n"
            f"Maintenance Excess: ${balances.maintenance_excess:,.2f}"
        )
    except Exception as e:
        return f"Error fetching balances: {str(e)}"

@mcp.tool()
async def get_open_positions() -> str:
    """Get all currently open positions in the trading account."""
    try:
        positions = await account_state.get_positions()
        if not positions:
            return "No open positions found."

        headers = ["Symbol", "Type", "Quantity", "Mark Price", "Value"]
        table_data = []

        for pos in positions:
            value = float(pos.mark_price or 0) * float(pos.quantity) * pos.multiplier
            table_data.append([
                pos.symbol,
                pos.instrument_type,
                pos.quantity,
                f"${float(pos.mark_price or 0):,.2f}",
                f"${value:,.2f}"
            ])

        output = ["Current Positions:", ""]
        output.append(tabulate(table_data, headers=headers, tablefmt="grid"))
        return "\n".join(output)
    except Exception as e:
        return f"Error fetching positions: {str(e)}"

@mcp.tool()
def get_transaction_history(start_date: str | None = None) -> str:
    """Get detailed transaction history for the account.

    Args:
        start_date: Optional start date in YYYY-MM-DD format (e.g., '2024-01-01').
        If not provided, returns last 90 days of transactions.

    Returns a formatted table showing:
    - Date: Transaction date
    - Sub Type: Transaction category
    - Description: Detailed transaction description
    - Value: Transaction amount in USD

    Returns:
        Formatted string containing table of transactions or message if none found
    """
    try:
        # Default to 90 days if no date provided
        if start_date is None:
            date_obj = date.today() - timedelta(days=90)
        else:
            try:
                date_obj = datetime.strptime(start_date, "%Y-%m-%d").date()
            except ValueError:
                return "Invalid date format. Please use YYYY-MM-DD (e.g., '2024-01-01')"

        transactions = account_state.account.get_history(account_state.session, start_date=date_obj)
        if not transactions:
            return "No transactions found for the specified period."

        headers = ["Date", "Sub Type", "Description", "Value"]
        table_data = []

        for txn in transactions:
            table_data.append([
                txn.transaction_date.strftime("%Y-%m-%d"),
                txn.transaction_sub_type or 'N/A',
                txn.description or 'N/A',
                f"${float(txn.net_value):,.2f}" if txn.net_value is not None else 'N/A'
            ])

        output = ["Transaction History:", ""]
        output.append(tabulate(table_data, headers=headers, tablefmt="grid"))
        return "\n".join(output)
    except Exception as e:
        return f"Error fetching transactions: {str(e)}"

@mcp.tool()
async def get_metrics(symbols: list[str]) -> str:
    """Get market metrics and analysis for specified stock symbols.

    Args:
        symbols: List of stock symbols to analyze (e.g., ["SPY", "AAPL"])

    Returns a formatted table showing for each symbol:
    - IV Rank: Implied volatility rank as percentage
    - IV %ile: Implied volatility percentile
    - Beta: Stock's beta value
    - Liquidity: Liquidity rating
    - Borrow Rate: Stock borrow rate if available
    - Next Earnings: Expected earnings date and time if available

    Returns:
        Formatted string containing table of metrics or message if none found
    """
    try:
        metrics_data = await metrics.a_get_market_metrics(account_state.session, symbols)
        if not metrics_data:
            return "No metrics found for the specified symbols."

        # Prepare data for tabulate
        headers = ["Symbol", "IV Rank", "IV %ile", "Beta", "Liquidity", "Borrow Rate"]
        table_data = []

        for m in metrics_data:
            # Convert values with proper error handling
            iv_rank = f"{float(m.implied_volatility_index_rank) * 100:.1f}%" if m.implied_volatility_index_rank else "N/A"
            iv_percentile = f"{float(m.implied_volatility_percentile) * 100:.1f}%" if m.implied_volatility_percentile else "N/A"
            beta = f"{float(m.beta):.2f}" if m.beta else "N/A"
            borrow_rate = f"{float(m.borrow_rate):.2f}%" if hasattr(m, 'borrow_rate') and m.borrow_rate else "N/A"

            row = [
                m.symbol,
                iv_rank,
                iv_percentile,
                beta,
                m.liquidity_rating or "N/A",
                borrow_rate
            ]
            table_data.append(row)

            # Add earnings info as a separate row if available
            if hasattr(m, 'earnings') and m.earnings:
                table_data.append([
                    f"↳ Next Earnings: {m.earnings.expected_report_date} ({m.earnings.time_of_day})",
                    "", "", "", "", ""
                ])

        output = ["Market Metrics:", ""]
        output.append(tabulate(table_data, headers=headers, tablefmt="grid"))
        return "\n".join(output)
    except Exception as e:
        return f"Error fetching market metrics: {str(e)}"

@mcp.tool()
async def get_prices(
    underlying_symbol: str,
    expiration_date: str | None = None,
    option_type: Literal["C", "P"] | None = None,
    strike: float | None = None,
) -> str:
    """Get current bid/ask prices for a stock or option. Note that scheduled trades will execute at the market price, not the price returned by this tool.

    Args:
        underlying_symbol: Underlying symbol (e.g., "SPY", "AAPL")
        expiration_date: Optional expiration date in YYYY-MM-DD format for options
        option_type: Optional option type ("C" for call, "P" for put)
        strike: Optional strike price

    Returns:
        String containing bid and ask prices
    """
    result = await get_prices_internal(underlying_symbol, expiration_date, option_type, strike)
    if isinstance(result, tuple):
        bid, ask = result
        return (
            f"Current prices for {underlying_symbol}:\n"
            f"Bid: ${float(bid):.2f}\n"
            f"Ask: ${float(ask):.2f}"
        )
    return result

def main():
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "setup":
            sys.exit(0 if auth() else 1)

        # Initialize account state
        account_state.session

        logger.info("Server is running")
        mcp.run()

    except Exception as e:
        logger.error(f"Error in running server: {e}")
        sys.exit(1)
