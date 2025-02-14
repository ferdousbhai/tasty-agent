import asyncio
from typing import Literal
import logging
from uuid import uuid4
import sys
import io
import base64
import matplotlib
import matplotlib.pyplot as plt
from datetime import date, timedelta, datetime

from tastytrade import metrics

from .auth_cli import auth
from .common import mcp, session, account
from .task import Task, scheduled_tasks

logger = logging.getLogger(__name__)


@mcp.tool()
async def schedule_trade(
    symbol: str,
    quantity: int,
    action: Literal["Buy to Open", "Sell to Close"],
    execution_type: Literal["immediate", "once", "daily"] = "immediate",
    run_time: str | None = None,
    dry_run: bool = True
) -> str:
    """Schedule a trade for execution.

    Args:
        symbol: The option or stock symbol to trade (e.g., "SPY", "AAPL220916C150")
        quantity: Number of contracts/shares to trade
        action: Trade direction - "Buy to Open" to open a new position or "Sell to Close" to close existing position
        execution_type: When to execute the trade:
            - "immediate": Execute as soon as market is open (default)
            - "once": Execute once at specified run_time
            - "daily": Execute every day at specified run_time
        run_time: Time to execute trade in HH:MM 24-hour format (e.g., "09:30"). Required for "once" and "daily" execution_type.
        dry_run: If True, simulate the trade without actually placing it (default is True)

    Returns:
        String containing task ID and confirmation message
    """
    try:
        # Validate run_time is provided when required
        if execution_type in ["once", "daily"] and not run_time:
            return "run_time parameter is required for 'once' and 'daily' execution types"

        # Validate run_time format if provided
        if run_time:
            try:
                hour, minute = map(int, run_time.split(":"))
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    return "Invalid time format. Please use HH:MM in 24-hour format"
            except ValueError:
                return "Invalid time format. Please use HH:MM format (e.g., '09:30')"

        # Create task and start execution
        task_id = str(uuid4())
        task = Task(
            task_id=task_id,
            symbol=symbol,
            quantity=quantity,
            action=action,
            dry_run=dry_run,
            description=f"{action} {quantity} {symbol}",
            schedule_type=execution_type,
            run_time=run_time
        )
        scheduled_tasks[task_id] = task
        task._task = asyncio.create_task(task.execute())  # Store the task
        return f"Task {task_id} scheduled successfully"

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
    """Plot the account's net liquidating value history and return as a base64 PNG image.

    Args:
        time_back: Time period to plot:
            - '1d': Last day
            - '1m': Last month
            - '3m': Last 3 months
            - '6m': Last 6 months
            - '1y': Last year
            - 'all': All available history

    Returns:
        Base64 encoded PNG image string of the portfolio value chart
    """
    history = account.get_net_liquidating_value_history(session, time_back=time_back)
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

@mcp.tool()
async def get_account_balances() -> str:
    """Get current account balances and buying power information."""
    try:
        balances = await account.a_get_balances(session)
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
        positions = await account.a_get_positions(session)
        if not positions:
            return "No open positions found."

        output = ["Current Positions:", ""]
        output.append(f"{'Symbol':<15} {'Type':<10} {'Quantity':<10} {'Value':<15}")
        output.append("-" * 50)

        for pos in positions:
            output.append(
                f"{pos.symbol:<15} {pos.instrument_type:<10} "
                f"{pos.quantity:<10} ${pos.value:,.2f}"
            )
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

        transactions = account.get_history(session, start_date=date_obj)
        if not transactions:
            return "No transactions found for the specified period."

        output = ["Transaction History:", ""]
        output.append(f"{'Date':<12} {'Sub Type':<15} {'Description':<45} {'Value':<15}")
        output.append("-" * 90)

        for txn in transactions:
            date_str = txn.transaction_date.strftime("%Y-%m-%d")
            sub_type = txn.transaction_sub_type or 'N/A'
            description = txn.description or 'N/A'
            value = f"${float(txn.net_value):,.2f}" if txn.net_value is not None else 'N/A'

            output.append(
                f"{date_str:<12} {sub_type:<15} {description:<45} {value:<15}"
            )
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
    - Next Earnings: Expected earnings date and time if available

    Returns:
        Formatted string containing table of metrics or message if none found
    """
    try:
        metrics_data = await metrics.a_get_market_metrics(session, symbols)
        if not metrics_data:
            return "No metrics found for the specified symbols."

        output = ["Market Metrics:", ""]
        output.append(f"{'Symbol':<6} {'IV Rank':<8} {'IV %ile':<8} {'Beta':<6} {'Liquidity':<10}")
        output.append("-" * 45)

        for m in metrics_data:
            iv_rank = f"{float(m.implied_volatility_index_rank * 100):.1f}%" if m.implied_volatility_index_rank else "N/A"
            iv_percentile = f"{float(m.implied_volatility_percentile * 100):.1f}%" if m.implied_volatility_percentile else "N/A"
            beta = f"{float(m.beta):.2f}" if m.beta else "N/A"

            output.append(
                f"{m.symbol:<6} {iv_rank:<8} {iv_percentile:<8} "
                f"{beta:<6} {m.liquidity_rating or 'N/A':<10}"
            )

            if m.earnings:
                output.append(f"  Next Earnings: {m.earnings.expected_report_date} ({m.earnings.time_of_day})")

        return "\n".join(output)
    except Exception as e:
        return f"Error fetching market metrics: {str(e)}"


def main():
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "setup":
            sys.exit(0 if auth() else 1)

        logger.info("Server is running")
        mcp.run()

    except Exception as e:
        logger.error(f"Error in main: {e}")
        sys.exit(1)
