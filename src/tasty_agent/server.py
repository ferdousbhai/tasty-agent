from datetime import timedelta, datetime, date
import logging
import sys
from typing import Literal
from uuid import uuid4

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.job import Job
from mcp.server.fastmcp import FastMCP
from tabulate import tabulate

from .tastytrade_api import TastytradeAPI
from .job_queue import JobQueue
from ..utils import is_market_open, format_time_until, get_next_market_open

logger = logging.getLogger(__name__)

# Tastytrade API
tastytrade_api = TastytradeAPI.get_instance()

# Initialize APScheduler
scheduler = AsyncIOScheduler(jobstores={'default': MemoryJobStore()})

# Initialize Job Queue
job_queue = JobQueue(scheduler)

# MCP Server
mcp = FastMCP("TastyTrade")

# Get a human-readable status for a scheduled job
def get_job_status(job: Job) -> str:
    """Get a human-readable status string for a job"""
    try:
        if not job or not hasattr(job, 'next_run_time') or job.next_run_time is None:
            return "Unknown"

        time_str = format_time_until(job.next_run_time)
        return f"Executing in {time_str}"
    except AttributeError:
        # Handle the case where next_run_time isn't available
        return "Pending (scheduler not started)"

@mcp.resource("file://scheduled_trades/tasks/list")
async def get_scheduled_trades():
    """List all pending scheduled trades with execution status and details."""
    try:
        jobs = scheduler.get_jobs()
        if not jobs:
            return "No scheduled trades"

        headers = ["Position", "ID", "Action", "Instrument", "Quantity", "Status"]
        rows = []

        for i, job in enumerate(jobs, 1):
            # Extract trade info from job kwargs
            kwargs = job.kwargs
            if not kwargs:
                continue

            action = kwargs.get('action')
            underlying = kwargs.get('underlying_symbol')
            quantity = kwargs.get('quantity')

            if not all([action, underlying, quantity]):
                continue

            # Build description
            description = kwargs.get('description')
            if not description:
                description = f"{action} {quantity} {underlying}"
                option_type = kwargs.get('option_type')
                strike = kwargs.get('strike')
                exp_date = kwargs.get('expiration_date')
                if option_type and strike and exp_date:
                    description += f" {option_type}{strike} exp {exp_date.strftime('%Y-%m-%d')}"

            rows.append([
                i, job.id, action, description, quantity, get_job_status(job)
            ])

        return tabulate(rows, headers, tablefmt="grid")
    except Exception as e:
        logger.error(f"Error retrieving scheduled trades: {e}", exc_info=True)
        return f"Error retrieving scheduled trades: {e}"

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
    """Schedule stock/option trade for immediate or market-open execution.

    Args:
        action: Buy to Open or Sell to Close
        quantity: Number of shares/contracts
        underlying_symbol: Stock ticker symbol
        strike: Option strike price (if option)
        option_type: C for Call, P for Put (if option)
        expiration_date: Option expiry in YYYY-MM-DD format (if option)
        dry_run: Test without executing if True
    """
    try:
        expiry_datetime = None
        if expiration_date:
            try:
                expiry_datetime = datetime.strptime(expiration_date, "%Y-%m-%d")
            except ValueError:
                return "Invalid expiration date format. Please use YYYY-MM-DD format"

        instrument = await tastytrade_api.create_instrument(
            underlying_symbol=underlying_symbol,
            expiration_date=expiry_datetime,
            option_type=option_type,
            strike=strike
        )
        if instrument is None:
            return f"Could not find instrument for symbol: {underlying_symbol}"

        # Create job ID and description
        job_id = str(uuid4())
        description = f"{action} {quantity} {underlying_symbol}"
        if option_type:
            description += f" {option_type}{strike} exp {expiration_date}"

        # Define the execute_trade function that will be called directly or scheduled
        async def execute_trade() -> tuple[bool, str]:
            """Execute the trade directly"""
            # Ensure we're in market hours before attempting execution
            if not is_market_open():
                logger.warning(f"Market closed, cannot execute trade (job: {job_id})")
                return False, "Market is closed"

            api = TastytradeAPI.get_instance()
            try:
                result = await api.place_trade(
                    instrument=instrument,
                    quantity=quantity,
                    action=action,
                    dry_run=dry_run
                )
                logger.info(f"Trade executed successfully (job: {job_id}): {result}")
                return True, str(result)
            except Exception as e:
                error_msg = f"Execution failed: {str(e)}"
                logger.error(f"Trade failed (job: {job_id}): {error_msg}")
                return False, error_msg

        if is_market_open():
            # Execute immediately
            success, message = await execute_trade()
            if success:
                return f"Trade executed immediately: {message}"
            else:
                return f"Trade execution failed: {message}"
        else:
            # Schedule for market open
            async def execute_scheduled_trade():
                try:
                    success, message = await execute_trade()
                    if success:
                        logger.info(f"Scheduled trade completed successfully (job: {job_id}): {message}")
                    else:
                        logger.error(f"Scheduled trade failed (job: {job_id}): {message}")
                except Exception as e:
                    logger.error(f"Error executing scheduled trade (job: {job_id}): {str(e)}")

            # Get the next market open time
            next_market_open = get_next_market_open()

            # Add the job to the queue for sequential execution
            await job_queue.add_job(
                job_func=execute_scheduled_trade,
                run_date=next_market_open,
                job_id=job_id
            )

            try:
                time_until = format_time_until(next_market_open)
                return f"Trade scheduled as job {job_id} - will execute at next market open ({next_market_open.strftime('%Y-%m-%d %H:%M:%S %Z')}): in {time_until}"
            except AttributeError:
                return f"Trade scheduled as job {job_id} - will execute at next market open ({next_market_open.strftime('%Y-%m-%d %H:%M:%S %Z')})"

    except Exception as e:
        logger.error(f"Error scheduling trade: {e}", exc_info=True)
        return f"Error scheduling trade: {str(e)}"

@mcp.tool()
async def remove_scheduled_trade(job_id: str) -> str:
    """Cancel a scheduled trade by its job ID.

    Args:
        job_id: The ID of the scheduled trade to remove
    """
    try:
        # Try to remove from queue first
        if job_queue.remove_job(job_id):
            return f"Successfully removed scheduled job from queue: {job_id}"
            
        # If not in queue, try to remove from scheduler
        job = scheduler.get_job(job_id)
        if not job:
            return f"No scheduled job found with ID: {job_id}"

        scheduler.remove_job(job_id)
        return f"Successfully removed scheduled job: {job_id}"
    except Exception as e:
        return f"Error removing scheduled job: {str(e)}"

@mcp.tool()
def plot_nlv_history(
    time_back: Literal['1d', '1m', '3m', '6m', '1y', 'all'] = '1y'
) -> str:
    """Generate a plot of account value history as base64 PNG.

    Args:
        time_back: Time period to plot (1d=1 day, 1m=1 month, etc.)
    """
    try:
        import io
        import base64
        import matplotlib
        import matplotlib.pyplot as plt

        try:
            history = tastytrade_api.get_nlv_history(time_back=time_back)
            if not history or len(history) == 0:
                return "No history data available for the selected time period."
        except Exception as e:
            logger.error(f"Error retrieving NLV history: {e}", exc_info=True)
            return f"Unable to retrieve portfolio history: {str(e)}"

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
    """Retrieve current account cash balance, buying power, and net liquidating value."""
    try:
        balances = await tastytrade_api.get_balances()
        return (
            f"Account Balances:\n"
            f"Cash Balance: ${balances.cash_balance:,.2f}\n"
            f"Buying Power: ${balances.derivative_buying_power:,.2f}\n"
            f"Net Liquidating Value: ${balances.net_liquidating_value:,.2f}\n"
            f"Maintenance Excess: ${balances.maintenance_excess:,.2f}"
        )
    except Exception as e:
        logger.error(f"Error in get_account_balances: {e}")
        return f"Error fetching balances: {str(e)}"

@mcp.tool()
async def get_open_positions() -> str:
    """List all currently open stock and option positions with current values."""
    try:
        positions = await tastytrade_api.get_positions()
        if not positions:
            return "No open positions found."

        headers = ["Symbol", "Type", "Quantity", "Mark Price", "Value"]
        table_data = []

        for pos in positions:
            # Process each position, skipping any that cause errors
            try:
                value = float(pos.mark_price or 0) * float(pos.quantity) * pos.multiplier
                table_data.append([
                    pos.symbol,
                    pos.instrument_type,
                    pos.quantity,
                    f"${float(pos.mark_price or 0):,.2f}",
                    f"${value:,.2f}"
                ])
            except Exception as e:
                logger.error(f"Error processing position {pos.symbol}: {e}")
                continue

        output = ["Current Positions:", ""]
        output.append(tabulate(table_data, headers=headers, tablefmt="plain"))
        return "\n".join(output)
    except Exception as e:
        logger.error(f"Error in get_open_positions: {e}")
        return f"Error fetching positions: {str(e)}"

@mcp.tool()
def get_transaction_history(start_date: str | None = None) -> str:
    """Get account transaction history from start_date (YYYY-MM-DD) or last 90 days."""
    try:
        # Default to 90 days if no date provided
        if start_date is None:
            date_obj = date.today() - timedelta(days=90)
        else:
            try:
                date_obj = datetime.strptime(start_date, "%Y-%m-%d").date()
            except ValueError:
                return "Invalid date format. Please use YYYY-MM-DD (e.g., '2024-01-01')"

        transactions = tastytrade_api.get_transaction_history(start_date=date_obj)
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
        output.append(tabulate(table_data, headers=headers, tablefmt="plain"))
        return "\n".join(output)
    except Exception as e:
        return f"Error fetching transactions: {str(e)}"

@mcp.tool()
async def get_metrics(symbols: list[str]) -> str:
    """Get market metrics for symbols (IV Rank, Beta, Liquidity, Earnings).

    Args:
        symbols: List of stock ticker symbols to get metrics for
    """
    try:
        metrics_data = await tastytrade_api.get_market_metrics(symbols)
        if not metrics_data:
            return "No metrics found for the specified symbols."

        headers = ["Symbol", "IV Rank", "IV %ile", "Beta", "Liquidity", "Lendability", "Earnings"]
        table_data = []

        for m in metrics_data:
            # Process each metric, skipping any that cause errors
            try:
                # Convert values with proper error handling
                iv_rank = f"{float(m.implied_volatility_index_rank) * 100:.1f}%" if m.implied_volatility_index_rank else "N/A"
                iv_percentile = f"{float(m.implied_volatility_percentile) * 100:.1f}%" if m.implied_volatility_percentile else "N/A"
                beta = f"{float(m.beta):.2f}" if m.beta else "N/A"

                earnings_info = "N/A"
                earnings = getattr(m, "earnings", None)
                if earnings is not None:
                    expected = getattr(earnings, "expected_report_date", None)
                    time_of_day = getattr(earnings, "time_of_day", None)
                    if expected is not None and time_of_day is not None:
                        earnings_info = f"{expected} ({time_of_day})"

                row = [
                    m.symbol,
                    iv_rank,
                    iv_percentile,
                    beta,
                    m.liquidity_rating or "N/A",
                    m.lendability or "N/A",
                    earnings_info
                ]
                table_data.append(row)
            except Exception as e:
                logger.error(f"Error processing metrics for {m.symbol}: {e}")
                continue

        output = ["Market Metrics:", ""]
        output.append(tabulate(table_data, headers=headers, tablefmt="plain"))
        return "\n".join(output)
    except Exception as e:
        logger.error(f"Error in get_metrics: {e}")
        return f"Error fetching market metrics: {str(e)}"

@mcp.tool()
async def get_prices(
    underlying_symbol: str,
    expiration_date: str | None = None,
    option_type: Literal["C", "P"] | None = None,
    strike: float | None = None,
) -> str:
    """Get current bid/ask prices for stock or option.

    Args:
        underlying_symbol: Stock ticker symbol
        expiration_date: Option expiry in YYYY-MM-DD format (for options)
        option_type: C for Call, P for Put (for options)
        strike: Option strike price (for options)
    """
    try:
        if expiration_date:
            try:
                datetime.strptime(expiration_date, "%Y-%m-%d")
            except ValueError:
                return "Invalid expiration date format. Please use YYYY-MM-DD format"

        result = await tastytrade_api.get_prices(underlying_symbol, expiration_date, option_type, strike)
        if isinstance(result, tuple):
            bid, ask = result
            return (
                f"Current prices for {underlying_symbol}:\n"
                f"Bid: ${float(bid):.2f}\n"
                f"Ask: ${float(ask):.2f}"
            )
        return result
    except Exception as e:
        logger.error(f"Error in get_prices for {underlying_symbol}: {e}")
        return f"Error getting prices: {str(e)}"

def main():
    from .auth_cli import auth
    import threading
    import time

    # Handle setup command
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        sys.exit(0 if auth() else 1)

    try:
        # Initialize API and ensure we have a valid session
        _ = tastytrade_api.session
        logger.info("Server is starting")

        # Define a function to start the scheduler after a short delay
        # This ensures MCP has time to initialize its event loop
        def delayed_scheduler_start():
            # Give MCP server a moment to initialize
            time.sleep(2)

            try:
                # Start the scheduler in a separate thread
                logger.info("Starting scheduler...")
                scheduler.start()
                logger.info("Scheduler started successfully")
            except Exception as e:
                logger.error(f"Error starting scheduler: {e}")
                # Don't exit, just log the error

        # Start the scheduler in a background thread
        threading.Thread(target=delayed_scheduler_start, daemon=True).start()

        # Run the MCP server - this will create its own event loop with anyio.run()
        logger.info("Starting MCP server...")
        mcp.run()

    except Exception as e:
        logger.error(f"Error in running server: {e}")
        # Attempt to shutdown the scheduler if it's running
        try:
            scheduler.shutdown()
            logger.info("Scheduler shut down")
        except Exception as shutdown_error:
            logger.error(f"Error shutting down scheduler: {shutdown_error}")
        sys.exit(1)
