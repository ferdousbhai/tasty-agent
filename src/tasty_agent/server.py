from datetime import timedelta, datetime, date
from typing import Literal, Any, AsyncIterator
from uuid import uuid4
import asyncio
from dataclasses import dataclass, field
from tabulate import tabulate
from contextlib import asynccontextmanager
import logging

from mcp.server.fastmcp import FastMCP, Context

from .tastytrade_client import TastytradeClient
from ..utils import is_market_open, format_time_until, get_next_market_open

logger = logging.getLogger(__name__)

tastytrade_client = TastytradeClient.get_instance()


@dataclass
class ScheduledTradeJob:
    job_id: str
    description: str
    status: Literal["scheduled", "processing", "cancelling", "cancelled", "completed", "failed"]
    trade_params: dict[str, Any]
    execution_task: asyncio.Task | None = None # Task for delayed execution
    created_at: datetime = field(default_factory=datetime.now)
    scheduled_execution_time: datetime | None = None # Informational only

# --- Define a dataclass for the lifespan state ---
@dataclass
class ServerContext:
    pending_trades: dict[str, ScheduledTradeJob]
    trade_execution_lock: asyncio.Lock

# --- Lifespan Handler ---
@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[ServerContext]:
    """Manages the trade state and lock lifecycle via context."""
    pending_trades = {}
    trade_execution_lock = asyncio.Lock()
    context = ServerContext(
            pending_trades=pending_trades,
            trade_execution_lock=trade_execution_lock,
    )
    try:
        yield context # Yield the ServerContext instance
    finally:
        # Cancel any pending trade tasks on shutdown
        tasks_to_cancel = [
            job.execution_task for job in context.pending_trades.values()
            if job.execution_task and not job.execution_task.done()
        ]
        if tasks_to_cancel:
            for task in tasks_to_cancel:
                task.cancel()
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True) # Wait for cancellations


# --- MCP Server ---
mcp = FastMCP("TastyTrade", lifespan=lifespan)


# --- Tools using the lifespan context ---
@mcp.tool()
async def schedule_trade(
    ctx: Context,
    action: Literal["Buy to Open", "Sell to Close"],
    quantity: int,
    underlying_symbol: str,
    strike: float | None = None,
    option_type: Literal["C", "P"] | None = None,
    expiration_date: str | None = None,
    dry_run: bool = False,
) -> str:
    """Schedule stock/option trade for immediate or sequential market-open execution.
    If market is closed, trade is scheduled for execution after next market open.
    Uses a lock to ensure sequential execution if multiple trades are pending.

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
        lifespan_ctx: ServerContext = ctx.request_context.lifespan_context
        pending_trades = lifespan_ctx.pending_trades
        trade_execution_lock = lifespan_ctx.trade_execution_lock
    except AttributeError:
        return "Error: Trade scheduling system state not accessible."

    try:
        if expiration_date:
            datetime.strptime(expiration_date, "%Y-%m-%d")
    except ValueError:
        return "Invalid expiration date format. Use YYYY-MM-DD."

    job_id = str(uuid4())
    desc_parts = [action, str(quantity), underlying_symbol]
    if option_type:
        desc_parts.extend([f"{option_type}{strike}", f"exp {expiration_date}"])
    description = " ".join(desc_parts)

    trade_params = {
        "underlying_symbol": underlying_symbol, "quantity": quantity, "action": action,
        "expiration_date": expiration_date, "option_type": option_type, "strike": strike,
        "dry_run": dry_run,
    }

    async def execute_trade(exec_job_id: str, exec_params: dict) -> tuple[bool, str]:
        """(Core) Acquires lock, executes the trade, releases lock.
        Assumes the caller has already checked job status and market open conditions.
        Returns (success, message).
        """
        async with trade_execution_lock: # Ensure sequential execution
            try:
                success, message = await tastytrade_client.place_trade(**exec_params, job_id=exec_job_id)
                return success, message
            except Exception as e:
                return False, f"Trade execution failed: {str(e)}"

    try:
        if is_market_open():
            # Market is open, execute immediately after acquiring lock
            # Create a placeholder job entry mainly for lock coordination & potential race conditions
            job = ScheduledTradeJob(
                 job_id=job_id, description=description, status="processing",
                 trade_params=trade_params, execution_task=None # No separate task needed
            )
            pending_trades[job_id] = job # Add temporarily
            execution_result = await execute_trade(job_id, trade_params)
            # Decide whether to keep the completed/failed job entry in pending_trades
            # For simplicity, let's remove it immediately after execution attempt
            if job_id in pending_trades:
                 del pending_trades[job_id]
            return execution_result
        else:
            # Market is closed, schedule delayed execution
            next_market_open = get_next_market_open()
            time_until = format_time_until(next_market_open)
            now_local = datetime.now().astimezone()
            wait_seconds = max(0, (next_market_open - now_local).total_seconds()) + 30 # Add buffer

            async def run_scheduled_trade(run_job_id: str, run_params: dict):
                """Task body: Waits for market open, checks status, then calls execute_trade."""
                job = None # Define job here for access in exception blocks
                try:
                    await asyncio.sleep(wait_seconds)

                    # Check job status *after* waking up
                    job = pending_trades.get(run_job_id)
                    if not job or job.status != "scheduled":
                        # Job was cancelled or doesn't exist anymore.
                        return # Exit quietly

                    # Brief check to ensure market is open (handles edge cases like holidays/short delays)
                    while not is_market_open():
                        await asyncio.sleep(30) # Wait if market didn't open exactly as expected

                    # Attempt execution
                    job.status = "processing"
                    success, message = await execute_trade(run_job_id, run_params)

                    # Update final status based on execution result
                    job.status = "completed" if success else "failed"
                    # No need to return message here, it's handled by the core execute_trade

                except asyncio.CancelledError:
                    # Handle cancellation initiated by cancel_scheduled_trade
                    job = pending_trades.get(run_job_id) # Re-fetch job reference
                    if job and job.status == "cancelling": # Confirm cancellation was intended
                        job.status = "cancelled"
                    # else: If status isn't cancelling, it was an unexpected cancel. Log or handle as needed.

                except Exception as e:
                    # Log unexpected errors during the wait or execution attempt
                    job = pending_trades.get(run_job_id) # Re-fetch job reference
                    logger.exception(f"Unexpected error in scheduled trade task {run_job_id}: {e}")
                    if job:
                        job.status = "failed"
                finally:
                    # Ensure task reference is cleared in all cases (completion, failure, cancellation)
                    if job:
                        job.execution_task = None

            # Create the job entry first
            job = ScheduledTradeJob(
                job_id=job_id, description=description, status="scheduled",
                trade_params=trade_params, scheduled_execution_time=next_market_open,
                execution_task=None # Will be set below
            )
            pending_trades[job_id] = job

            # Create and store the task
            delayed_task = asyncio.create_task(run_scheduled_trade(job_id, trade_params))
            job.execution_task = delayed_task # Store the task reference in the job

            return f"Market closed. Trade '{description}' scheduled as job {job_id}. Will execute after next market open (in {time_until})."

    except Exception as e:
        # General error during scheduling phase
        # Clean up if job was partially added
        if job_id in pending_trades and pending_trades[job_id].status in ["scheduled", "processing"]:
            if pending_trades[job_id].execution_task:
                pending_trades[job_id].execution_task.cancel()
            del pending_trades[job_id]
        return f"Error scheduling trade: {str(e)}"

@mcp.tool()
async def list_scheduled_trades(ctx: Context) -> str:
    """Lists currently scheduled or processing trades (Job ID and description)."""

    pending_trades = ctx.request_context.lifespan_context.pending_trades

    scheduled_trades = sorted(
        [job for job in pending_trades.values() if job.status == 'scheduled'],
        key=lambda j: j.created_at
    )
    processing_jobs = [
        job for job in pending_trades.values() if job.status == 'processing'
    ]

    if not scheduled_trades and not processing_jobs:
         return "No trades are currently scheduled or being processed."

    output_lines = []
    if processing_jobs:
         # Should ideally only be one processing due to the lock, but list just in case
         output_lines.append("Currently processing:")
         output_lines.extend([f"- Job {job.job_id}: {job.description}" for job in processing_jobs])
         output_lines.append("")

    if scheduled_trades:
        output_lines.append("Scheduled Trades (Waiting for Market Open or Execution Slot):")
        output_lines.extend([f"- Job {job.job_id}: {job.description}" for job in scheduled_trades])
        output_lines.append(f"\nTotal scheduled: {len(scheduled_trades)}")

    return "\n".join(output_lines)


@mcp.tool()
async def cancel_scheduled_trade(ctx: Context, job_id: str) -> str:
    """Cancel a trade that is currently scheduled via its Job ID.
    Only works for trades scheduled while the market was closed.
    """
    try:
        lifespan_ctx: ServerContext = ctx.request_context.lifespan_context
        pending_trades = lifespan_ctx.pending_trades
    except AttributeError:
        return "Error: Trade scheduling system state not accessible."

    job = pending_trades.get(job_id)

    if not job:
        return f"Error: Job ID '{job_id}' not found."

    # Check current status before attempting cancellation
    if job.status == "cancelled":
        return f"Job {job_id} ('{job.description}') is already cancelled."
    if job.status == "completed":
        return f"Error: Job {job_id} ('{job.description}') has already completed."
    if job.status == "failed":
        return f"Error: Job {job_id} ('{job.description}') has already failed."
    if job.status == "processing":
        return f"Error: Job {job_id} ('{job.description}') is already processing and cannot be cancelled."
    if job.status == "cancelling":
        return f"Job {job_id} ('{job.description}') is already being cancelled."

    # Only allow cancellation if the job is currently scheduled (and has a task)
    if job.status == "scheduled":
        task_to_cancel = job.execution_task
        if task_to_cancel and not task_to_cancel.done():
            try:
                job.status = "cancelling" # Mark as cancelling first
                task_to_cancel.cancel()
                # Wait briefly for the cancellation to be processed by the task wrapper
                await asyncio.sleep(0.1)
                # The task wrapper should update the status to "cancelled"
                if job.status == "cancelling": # If wrapper hasn't updated yet, force it
                     job.status = "cancelled"
                     job.execution_task = None
                return f"Trade job {job_id} ('{job.description}') has been cancelled."
            except Exception as e:
                # Revert status if cancellation failed unexpectedly
                job.status = "scheduled"
                return f"Error cancelling task for job {job_id}: {str(e)}"
        else:
            # Task doesn't exist or is already done, but status is scheduled? Inconsistent state.
            job.status = "failed" # Mark as failed due to inconsistency
            return f"Error: Job {job_id} is in state 'scheduled' but has no active execution task to cancel."
    else:
        # Should be unreachable due to checks above, but acts as a safeguard
        return f"Error: Job '{job_id}' cannot be cancelled. Status: {job.status}."

@mcp.tool()
async def plot_nlv_history(
    time_back: Literal['1d', '1m', '3m', '6m', '1y', 'all'] = '1y',
    show_web: bool = True
) -> str:
    """Generate a plot of account value history and display it via web browser.

    When show_web=True, this function returns a clickable URL.
    Please return this URL to the user so that they can click it to view the chart in their browser.

    Args:
        time_back: Time period to plot (1d=1 day, 1m=1 month, 3m=3 months, 6m=6 months, 1y=1 year, all=all time)
        show_web: Whether to display the plot in a web browser (default: True)
    """
    try:
        from . import chart_server

        # Get portfolio history data
        history = tastytrade_client.get_nlv_history(time_back=time_back)
        if not history or len(history) == 0:
            return "No history data available for the selected time period."

        # If web display is requested, use the chart server
        if show_web:
            try:
                chart_url = await chart_server.create_nlv_chart(history, time_back)
                return f"View your portfolio chart here:\n{chart_url}\n\nPortfolio value history for the past {time_back} is now available in your browser."
            except Exception as e:
                logger.error(f"Chart server encountered an error: {str(e)}")
                return f"Unable to display chart in web browser. The chart data has been processed but the web server encountered an error: {str(e)}"

        # Otherwise generate base64 image for direct display
        import io
        import base64
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

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
        logger.exception("Error generating plot")
        return f"Error generating plot: {str(e)}"

@mcp.tool()
async def get_account_balances() -> str:
    """Retrieve current account cash balance, buying power, and net liquidating value."""
    try:
        balances = await tastytrade_client.get_balances()
        return (
            f"Account Balances:\n"
            f"Cash Balance: ${balances.cash_balance:,.2f}\n"
            f"Buying Power: ${balances.derivative_buying_power:,.2f}\n"
            f"Net Liquidating Value: ${balances.net_liquidating_value:,.2f}\n"
            f"Maintenance Excess: ${balances.maintenance_excess:,.2f}"
        )
    except Exception as e:
        logger.exception("Error fetching balances")
        return f"Error fetching balances: {str(e)}"

@mcp.tool()
async def get_open_positions() -> str:
    """List all currently open stock and option positions with current values."""
    try:
        positions = await tastytrade_client.get_positions()
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
            except Exception:
                logger.warning("Skipping position due to processing error: %s", pos.symbol, exc_info=True)
                continue

        output = ["Current Positions:", ""]
        output.append(tabulate(table_data, headers=headers, tablefmt="plain"))
        return "\n".join(output)
    except Exception as e:
        logger.exception("Error fetching positions")
        return f"Error fetching positions: {str(e)}"

@mcp.tool()
def get_transaction_history(start_date: str | None = None) -> str:
    """Get account transaction history from start_date (YYYY-MM-DD) or last 90 days (if no date provided)."""
    try:
        # Default to 90 days if no date provided
        if start_date is None:
            date_obj = date.today() - timedelta(days=90)
        else:
            try:
                date_obj = datetime.strptime(start_date, "%Y-%m-%d").date()
            except ValueError:
                return "Invalid date format. Please use YYYY-MM-DD (e.g., '2024-01-01')"

        transactions = tastytrade_client.get_transaction_history(start_date=date_obj)
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
        logger.exception("Error fetching transaction history")
        return f"Error fetching transactions: {str(e)}"

@mcp.tool()
async def get_metrics(symbols: list[str]) -> str:
    """Get market metrics for symbols (IV Rank, Beta, Liquidity, Earnings)."""
    try:
        metrics_data = await tastytrade_client.get_market_metrics(symbols)
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
            except Exception:
                logger.warning("Skipping metric for symbol due to processing error: %s", m.symbol, exc_info=True)
                continue

        output = ["Market Metrics:", ""]
        output.append(tabulate(table_data, headers=headers, tablefmt="plain"))
        return "\n".join(output)
    except Exception as e:
        logger.exception("Error fetching market metrics")
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

        result = await tastytrade_client.get_prices(underlying_symbol, expiration_date, option_type, strike)
        if isinstance(result, tuple):
            bid, ask = result
            return (
                f"Current prices for {underlying_symbol}:\n"
                f"Bid: ${float(bid):.2f}\n"
                f"Ask: ${float(ask):.2f}"
            )
        return result
    except Exception as e:
        logger.exception("Error getting prices")
        return f"Error getting prices: {str(e)}"