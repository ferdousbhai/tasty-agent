from datetime import timedelta, datetime, date
import logging
from typing import Literal, Any
from uuid import uuid4
import asyncio

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.job import Job
from mcp.server.fastmcp import FastMCP
from tabulate import tabulate

from .tastytrade_api import TastytradeAPI
from ..utils import is_market_open, format_time_until, get_next_market_open

logger = logging.getLogger(__name__)


tastytrade_api = TastytradeAPI.get_instance()

scheduler = BackgroundScheduler()

# Using Python's built-in ordered dictionary as queue for FIFO processing (Python 3.7+)
trade_queue: dict[str, dict[str, Any]] = {}

# MCP Server
mcp = FastMCP("TastyTrade")

# Get a human-readable status for a scheduled job
def get_job_status(job: Job) -> str:
    """Get a human-readable status string for a job"""
    try:
        if not job or not hasattr(job, 'next_run_time') or job.next_run_time is None:
            return "Unknown"

        # Format: "Scheduled: Running in X minutes/hours/etc."
        return f"Scheduled: {format_time_until(job.next_run_time)}"
    except Exception as e:
        logger.error(f"Error getting job status: {e}")
        return "Status Error"

# Function to process the trade queue at market open
async def process_trade_queue():
    """Process all trades in the queue sequentially when market opens"""
    logger.info(f"Starting to process trade queue with {len(trade_queue)} pending trades")

    try:
        # Process all trades in sequence using a copy of the keys
        for job_id in list(trade_queue.keys()):
            if job_id not in trade_queue:  # Skip already removed jobs
                continue

            job_data = trade_queue[job_id]
            logger.info(f"Executing queued trade: {job_id}")

            try:
                # Execute trade with the new method signature
                success, result = await tastytrade_api.place_trade(
                    underlying_symbol=job_data.get('underlying_symbol'),
                    quantity=job_data.get('quantity'),
                    action=job_data.get('action'),
                    expiration_date=job_data.get('expiration_date'),
                    option_type=job_data.get('option_type'),
                    strike=job_data.get('strike'),
                    dry_run=job_data.get('dry_run', False),
                    job_id=job_id
                )
                logger.info(f"Trade executed: {result}")
            except Exception as e:
                logger.error(f"Error executing queued trade {job_id}: {e}")
            finally:
                # Remove the job from queue
                trade_queue.pop(job_id, None)  # Safely remove if exists

            # Small delay between trades
            await asyncio.sleep(1)

    except Exception as e:
        logger.error(f"Error processing trade queue: {e}")

    # Clear any remaining jobs
    trade_queue.clear()
    logger.info("Finished processing trade queue")

@mcp.tool()
async def list_scheduled_trades() -> str:
    """List all pending scheduled trades with execution status and details."""
    try:
        if not trade_queue:
            return "No scheduled trades"

        rows = []
        next_market_open = get_next_market_open()
        market_open_str = f"Queued for next market open (in {format_time_until(next_market_open)})"

        for i, (job_id, job_data) in enumerate(trade_queue.items(), 1):
            # Build instrument description
            instrument = job_data.get('underlying_symbol', '')
            if all(job_data.get(k) for k in ['option_type', 'strike', 'expiration_date']):
                exp_date = job_data['expiration_date']
                exp_str = exp_date.strftime('%Y-%m-%d') if hasattr(exp_date, 'strftime') else exp_date
                instrument += f" {job_data['option_type']}{job_data['strike']} exp {exp_str}"

            rows.append([
                i,
                job_id,
                job_data.get('action', ''),
                instrument,
                job_data.get('quantity', ''),
                market_open_str
            ])

        return tabulate(rows, ["Position", "ID", "Action", "Instrument", "Quantity", "Status"], tablefmt="plain")
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
    """Schedule stock/option trade for immediate or market-open execution. Trade will be executed at next market open if market is closed (at best available price).
    Scheduled trades only persist while the server is running. If the server restarts, pending trades will be lost.
    When scheduling multiple trades, the order of execution is FIFO.

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
        # Validate expiration date format if provided
        if expiration_date:
            try:
                # Just validate the format, we'll pass the string directly to place_trade
                datetime.strptime(expiration_date, "%Y-%m-%d")
            except ValueError:
                return "Invalid expiration date format. Please use YYYY-MM-DD format"

        # Create job ID and description
        job_id = str(uuid4())
        description = f"{action} {quantity} {underlying_symbol}"
        if option_type:
            description += f" {option_type}{strike} exp {expiration_date}"

        # If market is open, execute immediately
        if is_market_open():
            success, message = await tastytrade_api.place_trade(
                underlying_symbol=underlying_symbol,
                quantity=quantity,
                action=action,
                expiration_date=expiration_date,
                option_type=option_type,
                strike=strike,
                dry_run=dry_run,
                job_id=job_id,
                check_market_open=False  # Already checked above
            )

            if success:
                return f"Trade executed immediately: {message}"
            else:
                return f"Trade execution failed: {message}"

        # If market is closed, queue for next market open
        else:
            # Prepare job data for the queue with all necessary info
            job_data = {
                'job_id': job_id,
                'action': action,
                'quantity': quantity,
                'underlying_symbol': underlying_symbol,
                'expiration_date': expiration_date,
                'option_type': option_type,
                'strike': strike,
                'description': description,
                'dry_run': dry_run
            }

            # Add job to queue
            trade_queue[job_id] = job_data

            # Schedule a job to process the queue at market open if not already scheduled
            next_market_open = get_next_market_open()
            queue_processor_job = scheduler.get_job('market_open_queue_processor')
            if not queue_processor_job:
                scheduler.add_job(
                    process_trade_queue,
                    'date',
                    run_date=next_market_open,
                    id='market_open_queue_processor'
                )
                logger.info(f"Scheduled queue processor to run at market open: {next_market_open}")

            time_until = format_time_until(next_market_open)
            return f"Trade queued as job {job_id} - will execute at next market open ({next_market_open.strftime('%Y-%m-%d %H:%M:%S')}): in {time_until}"

    except Exception as e:
        logger.error(f"Error scheduling trade: {e}", exc_info=True)
        return f"Error scheduling trade: {str(e)}"

@mcp.tool()
async def remove_scheduled_trade(job_id: str) -> str:
    """Cancel a scheduled trade by its job ID."""
    try:
        if job_id in trade_queue:
            del trade_queue[job_id]
            return f"Successfully removed scheduled job from queue: {job_id}"
    except Exception as e:
        return f"Error removing scheduled job: {str(e)}"

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

def main():
    try:
        scheduler.start() # background scheduler
        mcp.run()
    finally:
        if scheduler.running:
            scheduler.shutdown()