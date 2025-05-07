from datetime import timedelta, datetime, date
from typing import Literal, Any, AsyncIterator
from uuid import uuid4
import asyncio
from dataclasses import dataclass, field
from tabulate import tabulate
from contextlib import asynccontextmanager
import logging
from zoneinfo import ZoneInfo
import keyring
import os
from decimal import Decimal

from mcp.server.fastmcp import FastMCP, Context
from tastytrade import Session, Account
from tastytrade.order import NewOrder, OrderStatus, OrderAction, OrderTimeInForce, OrderType, Leg
from tastytrade.instruments import Option, Equity, NestedOptionChain
from tastytrade.streamer import DXLinkStreamer
from tastytrade.dxfeed import Quote
from tastytrade import metrics

from ..utils import is_market_open, format_time_until, get_next_market_open

logger = logging.getLogger(__name__)


@dataclass
class ScheduledTradeJob:
    job_id: str
    description: str
    status: Literal["scheduled", "processing", "cancelling", "cancelled", "completed", "failed"]
    trade_params: dict[str, Any]
    execution_task: asyncio.Task | None = None
    created_at: datetime = field(default_factory=datetime.now)
    scheduled_execution_time: datetime | None = None

@dataclass
class ServerContext:
    pending_trades: dict[str, ScheduledTradeJob]
    trade_execution_lock: asyncio.Lock
    session: Session | None
    account: Account | None

@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[ServerContext]:
    """Manages the trade state, lock, and Tastytrade session lifecycle."""
    try:
        username = keyring.get_password("tastytrade", "username") or os.getenv("TASTYTRADE_USERNAME")
        password = keyring.get_password("tastytrade", "password") or os.getenv("TASTYTRADE_PASSWORD")
        account_id = keyring.get_password("tastytrade", "account_id") or os.getenv("TASTYTRADE_ACCOUNT_ID")

        if not username or not password:
            raise ValueError(
                "Missing Tastytrade credentials. Please run 'tasty-agent setup' or set "
                "TASTYTRADE_USERNAME and TASTYTRADE_PASSWORD environment variables."
            )

        tasty_session = Session(username, password)
        accounts = Account.get(tasty_session)

        if account_id:
            tasty_account = next((acc for acc in accounts if acc.account_number == account_id), None)
            if not tasty_account:
                raise ValueError(f"Specified Tastytrade account ID '{account_id}' not found.")
        else:
            tasty_account = accounts[0]
            if len(accounts) > 1:
                logger.warning(f"No TASTYTRADE_ACCOUNT_ID specified. Multiple accounts found. Using first found account: {tasty_account.account_number}")
            else:
                logger.info(f"Using Tastytrade account: {tasty_account.account_number}")

        context = ServerContext(
            pending_trades={},
            trade_execution_lock=asyncio.Lock(),
            session=tasty_session,
            account=tasty_account,
        )

        yield context

    finally:
        logger.info("Cleaning up lifespan resources...")
        if context:
            tasks_to_cancel = [
                job.execution_task for job in context.pending_trades.values()
                if job.execution_task and not job.execution_task.done()
            ]
            if tasks_to_cancel:
                for task in tasks_to_cancel:
                    task.cancel()
                await asyncio.gather(*tasks_to_cancel, return_exceptions=True) # Wait for cancellations
                logger.info(f"{len(tasks_to_cancel)} pending trade tasks cancelled.")


mcp = FastMCP("TastyTrade", lifespan=lifespan)


# --- Helper Functions ---
async def _create_instrument(
    session: Session,
    underlying_symbol: str,
    expiration_date: datetime | None = None,
    option_type: Literal["C", "P"] | None = None,
    strike: float | None = None,
) -> Option | Equity | None:
    """(Helper) Create an instrument object for a given symbol."""
    # If no option parameters, treat as equity
    if not any([expiration_date, option_type, strike]):
        return await Equity.a_get(session, underlying_symbol)

    # Validate all option parameters are present
    if not all([expiration_date, option_type, strike]):

        logger.error("Must provide all option parameters (expiration_date, option_type, strike) or none")
        return None

    # Get option chain
    chain: list[NestedOptionChain] = await NestedOptionChain.a_get(session, underlying_symbol)
    if not chain:
        logger.error(f"No option chain found for {underlying_symbol}")
        return None
    option_chain = chain[0]

    # Find matching expiration
    exp_date = expiration_date.date()
    expiration = next(
        (exp for exp in option_chain.expirations
        if exp.expiration_date == exp_date),
        None
    )
    if not expiration:
        logger.error(f"No expiration found for date {exp_date} in chain for {underlying_symbol}")
        return None

    # Find matching strike
    strike_obj = next(
        (s for s in expiration.strikes
        if float(s.strike_price) == strike),
        None
    )
    if not strike_obj:
        logger.error(f"No strike found for {strike} on {exp_date} in chain for {underlying_symbol}")
        return None

    # Get option symbol based on type
    option_symbol = strike_obj.call if option_type == "C" else strike_obj.put
    return await Option.a_get(session, option_symbol)

async def _get_quote(session: Session, streamer_symbol: str) -> tuple[Decimal, Decimal] | str:
    """(Helper) Get current quote for a symbol via DXLinkStreamer."""
    try:
        async with DXLinkStreamer(session) as streamer:
            await streamer.subscribe(Quote, [streamer_symbol])
            quote = await asyncio.wait_for(streamer.get_event(Quote), timeout=10.0)
            return Decimal(str(quote.bid_price)), Decimal(str(quote.ask_price))
    except asyncio.TimeoutError:
        logger.warning(f"Timed out waiting for quote data for {streamer_symbol}")
        return f"Timed out waiting for quote data for {streamer_symbol}"
    except asyncio.CancelledError:
        # Handle WebSocket cancellation explicitly
        logger.warning(f"WebSocket connection interrupted for {streamer_symbol}")
        return f"WebSocket connection interrupted for {streamer_symbol}"

async def _execute_trade_with_monitoring(
    session: Session,
    account: Account,
    underlying_symbol: str,
    quantity: int,
    action: Literal["Buy to Open", "Sell to Close"],
    expiration_date: str | None = None,
    option_type: Literal["C", "P"] | None = None,
    strike: float | None = None,
    dry_run: bool = False,
    job_id: str | None = None, # For logging
) -> tuple[bool, str]:
    """(Helper) Core logic for placing, retrying, and monitoring a trade."""
    log_prefix = f"[Job: {job_id}] " if job_id else ""
    original_requested_quantity = quantity

    try:
        expiry_datetime = None
        if expiration_date:
            try:
                expiry_datetime = datetime.strptime(expiration_date, "%Y-%m-%d")
            except ValueError as e:
                raise ValueError(f"Invalid expiration date format: {e}. Use YYYY-MM-DD format.")

        instrument = await _create_instrument(
            session=session,
            underlying_symbol=underlying_symbol,
            expiration_date=expiry_datetime,
            option_type=option_type,
            strike=strike
        )
        if instrument is None:
            error_msg = f"Could not create instrument for symbol: {underlying_symbol}"
            if expiry_datetime:
                error_msg += f" with expiration {expiration_date}, type {option_type}, strike {strike}"
            raise ValueError(error_msg)

        # --- Price Fetching ---
        quote_result = await _get_quote(session, instrument.streamer_symbol)
        if not isinstance(quote_result, tuple):
            raise ValueError(f"Failed to get price for {instrument.symbol}: {quote_result}")
        bid, ask = quote_result
        price_decimal = ask if action == "Buy to Open" else bid
        price_float = float(price_decimal)

        # --- Pre-Trade Checks --- (Adapted from place_trade)
        try:
            if action == "Buy to Open":
                multiplier = instrument.multiplier if hasattr(instrument, 'multiplier') else 1
                balances = await account.a_get_balances(session)
                order_value = price_decimal * Decimal(str(quantity)) * Decimal(str(multiplier))

                buying_power = (
                    balances.derivative_buying_power
                    if isinstance(instrument, Option)
                    else balances.equity_buying_power
                )

                if order_value > buying_power:
                    adjusted_quantity = int(buying_power / (price_decimal * Decimal(str(multiplier))))
                    if adjusted_quantity <= 0:
                        raise ValueError(f"Order rejected: Insufficient buying power (${buying_power:,.2f}) for even 1 unit @ ${price_float:.2f} (Value: ${price_decimal * Decimal(str(multiplier)):,.2f})")
                    logger.warning(
                        f"{log_prefix}Reduced order quantity from {original_requested_quantity} to {adjusted_quantity} "
                        f"due to buying power limit (${buying_power:,.2f} < ${order_value:,.2f})"
                    )
                    quantity = adjusted_quantity

            else:  # Sell to Close
                positions = await account.a_get_positions(session)
                position = next((p for p in positions if p.symbol == instrument.symbol), None)
                if not position:
                    raise ValueError(f"No open position found for {instrument.symbol}")

                live_orders = await account.a_get_live_orders(session)
                pending_sell_quantity = sum(
                    sum(leg.quantity for leg in order.legs if leg.symbol == instrument.symbol)
                    for order in live_orders
                    if order.status in (OrderStatus.LIVE, OrderStatus.RECEIVED) and
                       order.legs and
                       order.legs[0].action == OrderAction.SELL_TO_CLOSE
                )

                available_quantity = position.quantity - pending_sell_quantity
                logger.info(
                    f"{log_prefix}Position: {position.quantity}, Pending sells: {pending_sell_quantity}, Available: {available_quantity}"
                )

                if available_quantity <= 0:
                    raise ValueError(
                        f"Cannot place order - entire position of {position.quantity} "
                        f"already has pending sell orders ({pending_sell_quantity})"
                    )

                if quantity > available_quantity:
                    logger.warning(
                        f"{log_prefix}Reducing sell quantity from {original_requested_quantity} to {available_quantity} (maximum available)"
                    )
                    quantity = available_quantity

                if quantity <= 0:
                     raise ValueError(f"Calculated available quantity ({available_quantity}) is zero or less.")

        except ValueError as pre_trade_error:
             raise pre_trade_error # Re-raise to be caught by the outer handler

    except ValueError as setup_or_check_error:
        # Catch errors from instrument creation, pricing, or pre-trade checks
        logger.error(f"{log_prefix}{str(setup_or_check_error)}")
        return False, f"Trade setup/check error: {str(setup_or_check_error)}"

    # --- Order Placement with Retry Logic --- (Adapted from place_trade)
    max_placement_retries = 10
    placed_order_response = None
    final_quantity = quantity

    for attempt in range(max_placement_retries + 1):
        current_attempt_quantity = quantity

        if current_attempt_quantity <= 0:
            error_msg = f"Cannot place order, quantity reduced to zero during placement attempts (Attempt {attempt+1})."
            logger.error(f"{log_prefix}{error_msg}")
            return False, "Order rejected: Exceeds available funds after adjustments for fees/margin."

        # Build Leg and Order Details for Current Attempt
        order_action_enum = OrderAction.BUY_TO_OPEN if action == "Buy to Open" else OrderAction.SELL_TO_CLOSE
        try:
            leg: Leg = instrument.build_leg(current_attempt_quantity, order_action_enum)
        except Exception as build_leg_error:
            error_msg = f"Error building order leg for {instrument.symbol} (Qty: {current_attempt_quantity}): {build_leg_error}"
            logger.exception(f"{log_prefix}{error_msg}")
            return False, error_msg

        logger.info(
            f"{log_prefix}Attempting order placement (Attempt {attempt+1}/{max_placement_retries+1}): "
            f"{action} {current_attempt_quantity} {instrument.symbol} @ ${price_float:.2f}"
        )

        current_order_details = NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=OrderType.LIMIT,
            legs=[leg],
            price=price_decimal * (-1 if action == "Buy to Open" else 1)
        )

        # Attempt to Place Order
        response = await account.a_place_order(session, current_order_details, dry_run=dry_run)

        if not response.errors:
            placed_order_response = response
            final_quantity = current_attempt_quantity
            logger.info(f"{log_prefix}Order placement successful for quantity {final_quantity} (ID: {response.order.id if not dry_run and response.order else 'N/A - Dry Run'})")
            break # Exit placement loop
        else:
            is_insufficient_funds_error = any(
                "buying power" in str(e).lower() or
                "insufficient funds" in str(e).lower() or
                "margin requirement" in str(e).lower()
                for e in response.errors
            )

            if (action == "Buy to Open" and
                is_insufficient_funds_error and
                quantity > 1 and
                attempt < max_placement_retries):
                quantity -= 1
                logger.warning(
                    f"{log_prefix}Placement failed likely due to funds/fees. Errors: {response.errors}. "
                    f"Reducing quantity to {quantity} and retrying."
                )
                await asyncio.sleep(0.5) # Small delay
                continue
            else:
                error_msg = (f"Order placement failed permanently (Attempt {attempt+1}):\n"
                             + "\n".join(str(error) for error in response.errors))
                logger.error(f"{log_prefix}{error_msg}")
                return False, error_msg

    # After Placement Loop
    if not placed_order_response:
         error_msg = f"Order placement failed after {max_placement_retries + 1} attempts, likely due to persistent insufficient funds/fees."
         logger.error(f"{log_prefix}{error_msg}")
         return False, error_msg

    # Handle Dry Run Success
    if dry_run:
        msg = f"Dry run successful (Simulated: {action} {final_quantity} {instrument.symbol} @ ${price_float:.2f})"
        if placed_order_response.warnings:
            msg += "\nWarnings:\n" + "\n".join(str(w) for w in placed_order_response.warnings)
        logger.info(f"{log_prefix}{msg}")
        return True, msg

    # --- Live Order Monitoring (Post Successful Placement) --- (Adapted from place_trade)
    current_order = placed_order_response.order
    if not current_order:
         error_msg = "Order object not found in successful placement response."
         logger.error(f"{log_prefix}{error_msg}")
         return False, error_msg

    logger.info(f"{log_prefix}Monitoring placed order {current_order.id} (Qty: {final_quantity}) for fill...")

    # Prepare final leg for potential replacements
    final_order_action_enum = OrderAction.BUY_TO_OPEN if action == "Buy to Open" else OrderAction.SELL_TO_CLOSE
    try:
        final_leg: Leg = instrument.build_leg(final_quantity, final_order_action_enum)
    except Exception as build_leg_error:
        error_msg = f"Error building final order leg for replacement (Qty: {final_quantity}): {build_leg_error}"
        logger.exception(f"{log_prefix}{error_msg}")
        # Don't fail the whole process, but log that replacement might fail
        final_leg = None # Mark as None so replacement logic skips


    # Price Adjustment / Fill Monitoring Loop
    for fill_attempt in range(20):
        await asyncio.sleep(15.0)

        live_orders = await account.a_get_live_orders(session)
        order = next((o for o in live_orders if o.id == current_order.id), None)

        if not order:
            error_msg = f"Order {current_order.id} not found during monitoring. It might have filled or been cancelled."
            logger.warning(f"{log_prefix}{error_msg}")
            return False, error_msg # Treat as failure/uncertainty

        if order.status == OrderStatus.FILLED:
            success_msg = f"Order {order.id} filled successfully (Qty: {final_quantity})"
            logger.info(f"{log_prefix}{success_msg}")
            # Invalidate cache on success
            return True, success_msg

        if order.status not in (OrderStatus.LIVE, OrderStatus.RECEIVED):
            error_msg = f"Order {order.id} entered unexpected status during monitoring: {order.status}"
            logger.error(f"{log_prefix}{error_msg}")
            return False, error_msg # Terminal failure

        # Adjust Price if Still Live
        if not final_leg:
             logger.warning(f"{log_prefix}Cannot adjust order {order.id}, failed to build final leg earlier.")
             continue # Skip adjustment for this attempt

        price_delta = Decimal("0.01") if action == "Buy to Open" else Decimal("-0.01")
        try:
             current_price_decimal = Decimal(str(order.price))
        except Exception:
             error_msg = f"Could not parse current order price '{order.price}' as Decimal for adjustment."
             logger.error(f"{log_prefix}{error_msg}")
             continue # Skip adjustment

        new_price_decimal = current_price_decimal + price_delta
        logger.info(
            f"{log_prefix}Adjusting order price from ${current_price_decimal:.2f} to ${new_price_decimal:.2f} "
            f"(Fill Attempt {fill_attempt + 1}/20)"
        )

        replacement_order_details = NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=OrderType.LIMIT,
            legs=[final_leg],
            price=new_price_decimal * (-1 if action == "Buy to Open" else 1)
        )

        replace_response = await account.a_replace_order(session, order.id, replacement_order_details)
        if replace_response.errors:
            error_msg = f"Failed to adjust order {order.id}: {replace_response.errors}"
            logger.error(f"{log_prefix}{error_msg}")
        else:
             # Update reference if replacement creates a new order state/id (though unlikely)
             current_order = replace_response.order or current_order

    # Monitoring Loop Completed Without Fill
    final_msg = f"Order {current_order.id} not filled after 20 price adjustments. Attempting cancellation."
    logger.warning(f"{log_prefix}{final_msg}")

    # Attempt to delete the lingering order
    delete_result = await account.a_delete_order(session, current_order.id)
    logger.info(f"{log_prefix}Lingering order {current_order.id} cancellation result: {delete_result}")

    return False, final_msg # Return False because the trade did not fill

# --- MCP Server Tools ---

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
    lifespan_ctx: ServerContext = ctx.request_context.lifespan_context
    pending_trades = lifespan_ctx.pending_trades
    trade_execution_lock = lifespan_ctx.trade_execution_lock

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

    # --- Inner Execution Function (Handles Lock and Calls Helper) ---
    async def execute_trade_locked(exec_job_id: str, exec_params: dict) -> tuple[bool, str]:
        """Acquires lock, calls the main execution helper, releases lock."""
        async with trade_execution_lock: # Ensure sequential execution
            try:
                # Call the refactored trade execution logic
                success, message = await _execute_trade_with_monitoring(
                    session=lifespan_ctx.session,
                    account=lifespan_ctx.account,
                    job_id=exec_job_id,
                    **exec_params # Pass other params like symbol, qty, etc.
                )
                return success, message
            except Exception as e:
                logger.exception(f"[Job: {exec_job_id}] Unexpected error within execute_trade_locked")
                return False, f"Trade execution failed unexpectedly: {str(e)}"

    try:
        if is_market_open():
            # Market is open, execute immediately after acquiring lock
            job = ScheduledTradeJob(
                 job_id=job_id, description=description, status="processing",
                 trade_params=trade_params, execution_task=None
            )
            pending_trades[job_id] = job
            success, message = await execute_trade_locked(job_id, trade_params)
            # Remove from pending list regardless of outcome for immediate execution
            if job_id in pending_trades:
                del pending_trades[job_id]
            return message # Return the result message from execution
        else:
            # Market is closed, schedule delayed execution
            next_market_open = get_next_market_open()
            time_until = format_time_until(next_market_open)
            # Ensure consistent timezone for comparison
            now_ny = datetime.now(ZoneInfo('America/New_York'))
            wait_seconds = max(0, (next_market_open - now_ny).total_seconds()) + 30 # Add buffer

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

                    # Attempt execution via locked helper
                    job.status = "processing"
                    success, message = await execute_trade_locked(run_job_id, run_params)
                    logger.info(f"[Job: {run_job_id}] Scheduled execution result: {success} - {message}")

                    # Update final status based on execution result
                    job.status = "completed" if success else "failed"
                    job.execution_task = None # Clear task ref only after completion/failure

                except asyncio.CancelledError:
                    # Handle cancellation initiated by cancel_scheduled_trade
                    job = pending_trades.get(run_job_id)
                    if job and job.status == "cancelling":
                        job.status = "cancelled"
                        logger.info(f"[Job: {run_job_id}] Scheduled trade task successfully cancelled.")
                    else:
                        logger.warning(f"[Job: {run_job_id}] Scheduled trade task received unexpected CancelledError (status: {job.status if job else 'N/A'}).")
                    # Ensure status is terminal if cancelled unexpectedly
                    if job and job.status not in ["cancelled", "completed", "failed"]:
                         job.status = "failed"

                except Exception as e:
                    job = pending_trades.get(run_job_id)
                    logger.exception(f"Unexpected error in scheduled trade task {run_job_id}: {e}")
                    if job:
                        job.status = "failed"
                finally:
                    # Clear task reference in finally block for robustness, only if not already cleared
                    job = pending_trades.get(run_job_id)
                    if job and job.execution_task:
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
async def cancel_scheduled_trade(ctx: Context, job_id: str) -> str:
    """Cancel a trade that is currently scheduled via its Job ID.
    Only works for trades scheduled while the market was closed.
    """
    pending_trades = ctx.request_context.lifespan_context.pending_trades
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
async def list_scheduled_trades(ctx: Context) -> str:
    """Lists currently scheduled or processing trades (Job ID and description)."""
    pending_trades_values = ctx.request_context.lifespan_context.pending_trades.values()

    processing_jobs = [job for job in pending_trades_values if job.status == 'processing']
    scheduled_trades_list = sorted(
        [job for job in pending_trades_values if job.status == 'scheduled'],
        key=lambda j: j.created_at
    )

    if not processing_jobs and not scheduled_trades_list:
        return "No trades are currently scheduled or being processed."

    output_sections = []

    if processing_jobs:
        current_section_lines = ["Currently processing:"]
        current_section_lines.extend(f"- Job {job.job_id}: {job.description}" for job in processing_jobs)
        output_sections.append("\n".join(current_section_lines))

    if scheduled_trades_list:
        current_section_lines = ["Scheduled Trades (Waiting for Market Open or Execution Slot):"]
        current_section_lines.extend(f"- Job {job.job_id}: {job.description}" for job in scheduled_trades_list)
        current_section_lines.append(f"\nTotal scheduled: {len(scheduled_trades_list)}")
        output_sections.append("\n".join(current_section_lines))

    return "\n\n".join(output_sections)


# --- Tools (Data Retrieval - Reverted due to ctx injection bug) ---

@mcp.tool()
async def get_nlv_history(
    ctx: Context,
    time_back: Literal['1d', '1m', '3m', '6m', '1y', 'all'] = '1y'
) -> str:
    """Get Net Liquidating Value (NLV) history for the account.

    Returns the data as a formatted table with Date, Open, High, Low, and Close columns.

    Args:
        time_back: Time period for history (1d=1 day, 1m=1 month, 3m=3 months, 6m=6 months, 1y=1 year, all=all time)
    """
    lifespan_ctx: ServerContext = ctx.request_context.lifespan_context
    if not lifespan_ctx.session or not lifespan_ctx.account:
         return "Error: Tastytrade session not available. Check server logs."

    history = await lifespan_ctx.account.a_get_net_liquidating_value_history(lifespan_ctx.session, time_back=time_back)
    if not history or len(history) == 0:
        return "No history data available for the selected time period."

    # Format the data into a table
    headers = ["Date", "Open ($)", "High ($)", "Low ($)", "Close ($)"]
    # Store tuples of (date_object, formatted_date, open_str, high_str, low_str, close_str) for sorting
    parsed_data = []
    for n in history:
        # Parse the date part of the time string (first 10 chars)
        date_part = n.time[:10]
        sort_key_date = datetime.strptime(date_part, "%Y-%m-%d").date()

        # Format the date and OHLC values (using total_* fields)
        formatted_date = sort_key_date.strftime("%Y-%m-%d")
        open_str = f"{float(n.total_open):,.2f}"
        high_str = f"{float(n.total_high):,.2f}"
        low_str = f"{float(n.total_low):,.2f}"
        close_str = f"{float(n.total_close):,.2f}" # Use total_close for NLV
        parsed_data.append((sort_key_date, formatted_date, open_str, high_str, low_str, close_str))

    # Sort by date object descending (most recent first)
    parsed_data.sort(key=lambda item: item[0], reverse=True)

    # Format for tabulate *after* sorting
    table_data = [
        [formatted_date, open_str, high_str, low_str, close_str]
        for sort_key_date, formatted_date, open_str, high_str, low_str, close_str in parsed_data
    ]

    output = ["Net Liquidating Value History (Past {time_back}):", ""]
    output.append(tabulate(table_data, headers=headers, tablefmt="plain"))
    return "\n".join(output)

@mcp.tool()
async def get_account_balances(ctx: Context) -> str:
    """Retrieve current account cash balance, buying power, and net liquidating value.

    Note: Net Liquidating Value may be inaccurate when the market is closed.
    """
    lifespan_ctx: ServerContext = ctx.request_context.lifespan_context
    if not lifespan_ctx.session or not lifespan_ctx.account:
         return "Error: Tastytrade session not available. Check server logs."

    balances = await lifespan_ctx.account.a_get_balances(lifespan_ctx.session)
    return (
        f"Account Balances:\n"
        f"Cash Balance: ${float(balances.cash_balance):,.2f}\n"
        f"Equity Buying Power: ${float(balances.equity_buying_power):,.2f}\n"
        f"Derivative Buying Power: ${float(balances.derivative_buying_power):,.2f}\n"
        f"Net Liquidating Value: ${float(balances.net_liquidating_value):,.2f}\n"
        f"Maintenance Excess: ${float(balances.maintenance_excess):,.2f}"
    )

@mcp.tool()
async def get_current_positions(ctx: Context) -> str:
    """List all currently open stock and option positions with current values.

    Note: Mark price and calculated value may be inaccurate when the market is closed.
    """
    lifespan_ctx: ServerContext = ctx.request_context.lifespan_context

    positions = await lifespan_ctx.account.a_get_positions(lifespan_ctx.session, include_marks=True)
    if not positions:
        return "No open positions found."

    headers = ["Symbol", "Type", "Quantity", "Mark Price", "Value"]
    table_data = []

    for pos in positions:
        try:
            table_data.append([
                pos.symbol,
                pos.instrument_type,
                pos.quantity,
                f"${pos.mark_price:,.2f}",
                f"${pos.mark_price * pos.quantity * pos.multiplier:,.2f}"
            ])
        except Exception:
            logger.warning("Skipping position due to processing error: %s", pos.symbol, exc_info=True)
            continue

    output = ["Current Positions:", ""]
    output.append(tabulate(table_data, headers=headers, tablefmt="plain"))
    return "\n".join(output)

@mcp.tool()
async def get_transaction_history(
    ctx: Context,
    start_date: str | None = None
) -> str:
    """Get account transaction history from start_date (YYYY-MM-DD) or last 90 days (if no date provided)."""
    # Default to 90 days if no date provided
    if start_date is None:
        date_obj = date.today() - timedelta(days=90)
    else:
        try:
            date_obj = datetime.strptime(start_date, "%Y-%m-%d").date()
        except ValueError:
            return "Invalid date format. Please use YYYY-MM-DD (e.g., '2024-01-01')"

    lifespan_ctx: ServerContext = ctx.request_context.lifespan_context
    transactions = await lifespan_ctx.account.a_get_history(lifespan_ctx.session, start_date=date_obj)
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

@mcp.tool()
async def get_metrics(
    ctx: Context,
    symbols: list[str]
) -> str:
    """Get market metrics for symbols (IV Rank, Beta, Liquidity, Earnings)."""
    if not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols):
        return "Error: Input 'symbols' must be a list of strings."

    if not symbols:
        return "Error: No symbols provided."

    session = ctx.request_context.lifespan_context.session
    metrics_data = await metrics.a_get_market_metrics(session, symbols)
    if not metrics_data:
        return f"No metrics found for the specified symbols: {', '.join(symbols)}"

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

@mcp.tool()
async def get_prices(
    ctx: Context,
    underlying_symbol: str,
    expiration_date: str | None = None,
    option_type: Literal["C", "P"] | None = None,
    strike: float | None = None,
) -> str:
    """Get current bid/ask prices for stock or option.

    Note: When the market is closed, this may return stale data or fail if the data stream is unavailable.

    Args:
        underlying_symbol: Stock ticker symbol
        expiration_date: Option expiry in YYYY-MM-DD format (for options)
        option_type: C for Call, P for Put (for options)
        strike: Option strike price (for options)
    """

    expiry_datetime = None
    if expiration_date:
        try:
            expiry_datetime = datetime.strptime(expiration_date, "%Y-%m-%d")
        except ValueError:
            return "Invalid expiration date format. Please use YYYY-MM-DD format"

    session = ctx.request_context.lifespan_context.session
    instrument = await _create_instrument(
        session=session,
        underlying_symbol=underlying_symbol,
        expiration_date=expiry_datetime,
        option_type=option_type,
        strike=strike
    )

    if instrument is None:
        error_msg = f"Could not find instrument for: {underlying_symbol}"
        if expiry_datetime:
            error_msg += f" {expiry_datetime.strftime('%Y-%m-%d')} {option_type} {strike}"
        return error_msg

    streamer_symbol = instrument.streamer_symbol
    if not streamer_symbol:
        return f"Could not get streamer symbol for {instrument.symbol}"

    quote_result = await _get_quote(session, streamer_symbol)

    if isinstance(quote_result, tuple):
        bid, ask = quote_result
        return (
            f"Current prices for {instrument.symbol}:\n"
            f"Bid: ${bid:.2f}\n"
            f"Ask: ${ask:.2f}"
        )
    else:
        return quote_result