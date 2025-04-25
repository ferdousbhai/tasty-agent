from datetime import timedelta, datetime, date
import logging
from typing import Literal, Any, AsyncIterator
from uuid import uuid4
import asyncio
from dataclasses import dataclass, field
from tabulate import tabulate
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP, Context

from .tastytrade_api import TastytradeAPI
from ..utils import is_market_open, format_time_until, get_next_market_open

logger = logging.getLogger(__name__)


tastytrade_api = TastytradeAPI.get_instance()


@dataclass
class ScheduledTradeJob:
    job_id: str
    description: str
    status: Literal["queued", "processing", "cancelled", "completed", "failed"]
    trade_params: dict[str, Any]
    created_at: datetime = field(default_factory=datetime.now)
    scheduled_execution_time: datetime | None = None # Informational only


# --- Define a dataclass for the lifespan state ---
@dataclass
class ServerContext:
    trade_queue: asyncio.Queue[str]
    pending_trades: dict[str, ScheduledTradeJob]
    trade_processor_task: asyncio.Task | None


# --- Trade Processor Task ---
async def _trade_processor(queue: asyncio.Queue[str], trades: dict[str, ScheduledTradeJob]):
    """Processes trades sequentially. Waits for market open if closed."""
    logger.info("Trade processor task started.")
    while True:
        job_id = None # Reset job_id each iteration
        try:
            if is_market_open():
                logger.debug("Trade processor: Market is open. Checking queue...")
                # Market is open, poll queue quickly
                try:
                    # Use a short timeout to remain responsive to cancellation
                    # and allow the loop to re-check market status periodically.
                    job_id = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    # Queue is empty, loop again to check market status/cancellation
                    await asyncio.sleep(0.1) # Small sleep prevent tight loop on empty queue
                    continue
                except asyncio.CancelledError:
                     logger.info("Trade processor queue wait cancelled while market open.")
                     break # Exit loop if task is cancelled
            else:
                # Market is closed, wait until next open
                next_open = get_next_market_open()
                now = datetime.now(next_open.tzinfo)
                # Ensure wait_seconds is non-negative
                wait_seconds = max(0, (next_open - now).total_seconds()) + 5 # Add 5s buffer
                logger.info(f"Trade processor: Market closed. Waiting {format_time_until(next_open)} (~{wait_seconds:.0f}s) until next open.")
                try:
                    # Wait for the calculated duration OR a queue item (less likely)
                    # This wait_for allows the task to be cancelled during the wait.
                    job_id = await asyncio.wait_for(queue.get(), timeout=wait_seconds)
                except asyncio.TimeoutError:
                    # Waited until market open time, loop again to check market status
                    continue
                except asyncio.CancelledError:
                    logger.info("Trade processor queue wait cancelled while market closed.")
                    break # Exit loop if task is cancelled

            # --- Process the dequeued job (if one was received) ---
            if job_id is None: # Should not happen often, but safeguard
                continue

            job = trades.get(job_id)

            if not job or job.status == "cancelled":
                if job and job.status == "cancelled":
                    logger.info(f"Trade processor: Job {job_id} ('{job.description}') was cancelled. Removing.")
                    # Make sure deletion is safe if job is None (though `get` handles this)
                    if job_id in trades: del trades[job_id]
                elif not job:
                     logger.warning(f"Trade processor: Job ID {job_id} dequeued but not found in pending_trades. Skipping.")
                queue.task_done()
                continue

            logger.info(f"Trade processor: Processing job {job_id}: {job.description}")
            job.status = "processing"

            try:
                success, message = await tastytrade_api.place_trade(
                    **job.trade_params, job_id=job.job_id
                )
                new_status = "completed" if success else "failed"
                log_func = logger.info if success else logger.warning
                log_func(f"Trade processor: Job {job_id} {new_status}: {message}")
            except Exception as e:
                logger.error(f"Trade processor: Error executing trade for job {job_id} ('{job.description}'): {e}", exc_info=True)
                new_status = "failed"
            finally:
                 job.status = new_status
                 queue.task_done()
            # --- End job processing ---

        except asyncio.CancelledError:
            logger.info("Trade processor task received cancellation request during main loop.")
            break
        except Exception as e:
            # Catch broader errors in the main loop
            logger.error(f"Trade processor task encountered an unexpected error: {e}", exc_info=True)
            # Avoid busy-looping on persistent errors
            await asyncio.sleep(60)

    logger.info("Trade processor task finished.")


# --- Lifespan Handler ---
@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[ServerContext]:
    """Manages the trade queue and processor task lifecycle via context."""
    logger.info("Entering lifespan function...")
    processor_task = None
    state = None # Initialize state to None
    try:
        logger.info("Server lifespan startup: Initializing trade queue and processor.")
        trade_queue = asyncio.Queue()
        pending_trades = {}
        processor_task = asyncio.create_task(_trade_processor(trade_queue, pending_trades))
        logger.info("Server lifespan startup: Trade processor task created.")
        # Create an instance of the ServerContext dataclass
        state = ServerContext(
             trade_queue=trade_queue,
             pending_trades=pending_trades,
             trade_processor_task=processor_task,
        )
    except Exception as e:
        logger.error(f"ERROR DURING LIFESPAN STARTUP: {e}", exc_info=True)
        # If startup failed, yield a state with None task to avoid issues during cleanup
        state = ServerContext(trade_queue=asyncio.Queue(), pending_trades={}, trade_processor_task=None)


    try:
        yield state # Yield the ServerContext instance
    finally:
        logger.info("Server lifespan shutdown: Cancelling trade processor task.")
        # Access the task from the state if it was successfully created
        task_to_cancel = state.trade_processor_task if state else None
        if task_to_cancel and not task_to_cancel.done():
            task_to_cancel.cancel()
            try:
                await task_to_cancel
            except asyncio.CancelledError:
                logger.info("Server lifespan shutdown: Trade processor task successfully cancelled.")
            except Exception as e:
                logger.error(f"Server lifespan shutdown: Error during task cancellation: {e}", exc_info=True)
        elif task_to_cancel:
             logger.info("Server lifespan shutdown: Processor task already done.")
        else:
             logger.warning("Server lifespan shutdown: Processor task was not available or never created during startup.")
        logger.info("Server lifespan shutdown: Background task cleanup complete.")


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
    If market is closed, trade is queued. Relies on lifespan context for queue/state.

    Args:
        ctx: FastMCP context
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
        # Access state via attributes
        pending_trades = lifespan_ctx.pending_trades
        trade_queue = lifespan_ctx.trade_queue
    except AttributeError: # More specific exception
        logger.error("Lifespan context (ServerContext) not accessible via ctx.request_context.lifespan_context.")
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

    try:
        if is_market_open():
            logger.info(f"Market open. Executing immediately: {description} (Job ID: {job_id})")
            success, message = await tastytrade_api.place_trade(**trade_params, job_id=job_id)
            return f"Trade executed immediately: {message}" if success else f"Trade execution failed: {message}"
        else:
            next_market_open = get_next_market_open()
            time_until = format_time_until(next_market_open)
            logger.info(f"Market closed. Queuing: {description} (Job ID: {job_id}). Will process after open (in {time_until}).")
            job = ScheduledTradeJob(
                job_id=job_id, description=description, status="queued",
                trade_params=trade_params, scheduled_execution_time=next_market_open
            )
            # Use context variables (attributes)
            pending_trades[job_id] = job
            # Get the processor task and its loop from the context state
            processor_task = lifespan_ctx.trade_processor_task
            if not processor_task: # Ensure task exists
                 raise RuntimeError("Trade processor task not found in lifespan context.")
            processor_loop = processor_task.get_loop()
            # Schedule put operation on the processor's loop
            future = asyncio.run_coroutine_threadsafe(trade_queue.put(job_id), processor_loop)
            return f"Market closed. Trade '{description}' queued as job {job_id}. Will execute sequentially after next market open (in {time_until})."

    except Exception as e:
        logger.error(f"Error placing or queuing trade '{description}': {e}", exc_info=True)
        # Use lock for cleanup check (accessing via attribute)
        if job_id in lifespan_ctx.pending_trades: # Check before deleting
            del lifespan_ctx.pending_trades[job_id]
        return f"Error scheduling trade: {str(e)}"

@mcp.tool()
async def list_scheduled_trades(ctx: Context) -> str:
    """Lists currently queued trades (Job ID and description)."""
    try:
        lifespan_ctx: ServerContext = ctx.request_context.lifespan_context
        # Access state via attributes
        pending_trades = lifespan_ctx.pending_trades
    except AttributeError: # More specific exception
        logger.error("Lifespan context (ServerContext) not accessible via ctx.request_context.lifespan_context.")
        return "Error: Trade scheduling system state not accessible."

    queued_trades = sorted(
        [job for job in pending_trades.values() if job.status == 'queued'],
        key=lambda j: j.created_at
    )
    processing_job = next((job for job in pending_trades.values() if job.status == 'processing'), None)

    if not queued_trades and not processing_job:
         return "No trades are currently waiting in the queue or being processed."

    output_lines = []
    if processing_job:
         output_lines.append(f"Currently processing: Job {processing_job.job_id} ('{processing_job.description}')")
         output_lines.append("")

    if queued_trades:
        output_lines.append("Scheduled Trades Queue:")
        output_lines.extend([f"- Job {job.job_id}: {job.description}" for job in queued_trades])
        output_lines.append(f"\nTotal queued: {len(queued_trades)}")

    return "\n".join(output_lines)


@mcp.tool()
async def cancel_scheduled_trade(ctx: Context, job_id: str) -> str:
    """Cancel a trade that is currently queued via its Job ID."""
    try:
        lifespan_ctx: ServerContext = ctx.request_context.lifespan_context
        # Access state via attributes
        pending_trades = lifespan_ctx.pending_trades
    except AttributeError: # More specific exception
        logger.error("Lifespan context (ServerContext) not accessible via ctx.request_context.lifespan_context.")
        return "Error: Trade scheduling system state not accessible."

    job = pending_trades.get(job_id)
    if not job: return f"Error: Job ID '{job_id}' not found."
    if job.status != "queued": return f"Error: Job '{job_id}' cannot be cancelled. Status: {job.status}."
    job.status = "cancelled"
    logger.info(f"Marked job {job_id} ('{job.description}') as cancelled.")
    return f"Trade job {job_id} ('{job.description}') has been marked for cancellation."

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
        history = tastytrade_api.get_nlv_history(time_back=time_back)
        if not history or len(history) == 0:
            return "No history data available for the selected time period."

        # If web display is requested, use the chart server
        if show_web:
            try:
                chart_url = await chart_server.create_nlv_chart(history, time_back)
                return f"View your portfolio chart here:\n{chart_url}\n\nPortfolio value history for the past {time_back} is now available in your browser."
            except Exception as e:
                logger.error(f"Error with web chart: {e}", exc_info=True)
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
        logger.error(f"Error in plot_nlv_history: {e}", exc_info=True)
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
    """Get market metrics for symbols (IV Rank, Beta, Liquidity, Earnings)."""
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