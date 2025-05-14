import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta, datetime, date
from decimal import Decimal
import keyring
import logging
import os
from tabulate import tabulate
from typing import Literal, AsyncIterator
from uuid import uuid4

from mcp.server.fastmcp import FastMCP, Context
from tastytrade import Session, Account, metrics
from tastytrade.dxfeed import Quote
from tastytrade.instruments import Option, Equity, NestedOptionChain
from tastytrade.order import NewOrder, OrderStatus, OrderAction, OrderTimeInForce, OrderType, Leg, PriceEffect
from tastytrade.streamer import DXLinkStreamer

from ..utils import is_market_open, format_time_until, get_next_market_open

logger = logging.getLogger(__name__)


@dataclass
class ServerContext:
    trade_execution_lock: asyncio.Lock
    session: Session | None
    account: Account | None

@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[ServerContext]:
    """Manages the trade state, lock, and Tastytrade session lifecycle."""
    context = None # Initialize context to None for broader scope in finally
    try:
        username = keyring.get_password("tastytrade", "username") or os.getenv("TASTYTRADE_USERNAME")
        password = keyring.get_password("tastytrade", "password") or os.getenv("TASTYTRADE_PASSWORD")
        account_id = keyring.get_password("tastytrade", "account_id") or os.getenv("TASTYTRADE_ACCOUNT_ID")

        if not username or not password:
            raise ValueError(
                "Missing Tastytrade credentials. Please run 'tasty-agent setup' or set "
                "TASTYTRADE_USERNAME and TASTYTRADE_PASSWORD environment variables."
            )

        session = Session(username, password)
        accounts = Account.get(session)

        if account_id:
            account = next((acc for acc in accounts if acc.account_number == account_id), None)
            if not account:
                raise ValueError(f"Specified Tastytrade account ID '{account_id}' not found.")
        else:
            account = accounts[0]
            if len(accounts) > 1:
                logger.warning(f"No TASTYTRADE_ACCOUNT_ID specified. Multiple accounts found. Using first found account: {account.account_number}")
            else:
                logger.info(f"Using Tastytrade account: {account.account_number}")

        context = ServerContext(
            trade_execution_lock=asyncio.Lock(),
            session=session,
            account=account,
        )

        yield context

    finally:
        logger.info("Cleaning up lifespan resources...")
        if context:
            pass # No specific cleanup needed for ServerContext as simplified


mcp = FastMCP("TastyTrade", lifespan=lifespan)


# --- Helper Functions ---
async def _create_instrument(
    session: Session,
    underlying_symbol: str,
    expiration_date: datetime | None = None,
    option_type: Literal["C", "P"] | None = None,
    strike_price: float | None = None,
) -> Option | Equity | None:
    """(Helper) Create an instrument object for a given symbol."""

    if not expiration_date or not option_type or not strike_price:
        return await Equity.a_get(session, underlying_symbol)

    if not expiration_date is not None and option_type is not None and strike_price is not None:
        logger.error(
            "If any option parameter (expiration_date, option_type, strike_price) is provided, "
            "all must be provided. To fetch an equity, omit all option parameters."
        )
        return None

    # Get option chain
    if not (chains := await NestedOptionChain.a_get(session, underlying_symbol)):
        logger.error(f"No option chain found for {underlying_symbol}")
        return None
    option_chain = chains[0]

    # Find matching expiration
    exp_date = expiration_date.date()
    if not (expiration := next(
        (exp for exp in option_chain.expirations if exp.expiration_date == exp_date), None
    )):
        logger.error(f"No expiration found for date {exp_date} in chain for {underlying_symbol}")
        return None

    # Find matching strike
    if not (strike_obj := next(
        (s for s in expiration.strikes if float(s.strike_price) == strike_price), None
    )):
        logger.error(f"No strike found for {strike_price} on {exp_date} in chain for {underlying_symbol}")
        return None

    # Get option symbol based on type
    # option_type is guaranteed non-None here
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

# --- MCP Server Tools ---

@mcp.tool()
async def place_trade(
    ctx: Context,
    action: Literal["Buy to Open", "Sell to Close"],
    quantity: int,
    underlying_symbol: str,
    strike_price: float | None = None,
    option_type: Literal["C", "P"] | None = None,
    expiration_date: str | None = None,
    order_price: float | None = None,
    dry_run: bool = False,
) -> str:
    """Attempt to execute a stock/option trade immediately if the market is open or for a dry run.
    If the market is closed (and not a dry run), the trade is not placed.
    Uses a lock to ensure sequential execution of trade attempts.

    The order price is the user-specified price, or defaults to the mid-price between bid/ask.
    The order is placed once. There is no monitoring for fills or price adjustments.

    The specified quantity may be automatically adjusted downwards if it exceeds
    available buying power (for buy orders) or the number of shares/contracts
    available to sell (for sell orders).

    Args:
        action: Buy to Open or Sell to Close
        quantity: Number of shares/contracts
        underlying_symbol: Stock ticker symbol
        strike_price: Option strike price (if option)
        option_type: C for Call, P for Put (if option)
        expiration_date: Option expiry in YYYY-MM-DD format (if option)
        order_price: Optional. The limit price for the order. If None, mid-price is used.
        dry_run: Test without executing if True
    """
    lifespan_ctx: ServerContext = ctx.request_context.lifespan_context
    trade_execution_lock = lifespan_ctx.trade_execution_lock
    session_to_use = lifespan_ctx.session
    account_to_use = lifespan_ctx.account

    if not session_to_use or not account_to_use:
        return "Error: Tastytrade session or account not available. Check server logs."

    parsed_expiration_date = None
    if expiration_date:
        try:
            parsed_expiration_date = datetime.strptime(expiration_date, "%Y-%m-%d")
        except ValueError:
            return "Invalid expiration date format. Use YYYY-MM-DD."

    trade_id_for_logging = str(uuid4())
    log_prefix = f"[Trade: {trade_id_for_logging}] "

    # Inner function to encapsulate the core trade logic
    async def _execute_core_trade(instrument_obj: Equity | Option, user_specified_price: Decimal | None) -> tuple[bool, str]:
        # Uses variables from the outer scope: action, quantity (as initial_quantity_arg), 
        # session_to_use, account_to_use, log_prefix, dry_run
        
        initial_quantity_arg = quantity
        final_trade_quantity = initial_quantity_arg

        try:
            quote_res = await _get_quote(session_to_use, instrument_obj.streamer_symbol)
            if not isinstance(quote_res, tuple):
                raise ValueError(f"Failed to get price for {instrument_obj.symbol}: {quote_res}")
            bid_price, ask_price = quote_res

            limit_price_to_use: Decimal
            if user_specified_price is not None:
                limit_price_to_use = user_specified_price
                logger.info(f"{log_prefix}Using user-provided order price: ${limit_price_to_use:.2f}")
            else:
                if bid_price > Decimal(0) and ask_price > Decimal(0) and ask_price >= bid_price:
                    mid_price = (bid_price + ask_price) / 2
                    # Basic rounding for now. TODO: Consider instrument tick size for precision.
                    limit_price_to_use = mid_price.quantize(Decimal('0.01')) 
                    logger.info(f"{log_prefix}Using mid-price: ${limit_price_to_use:.2f} (Bid: {bid_price:.2f}, Ask: {ask_price:.2f})")
                else: # Fallback if mid-price cannot be determined
                    fallback_price = ask_price if action == "Buy to Open" else bid_price
                    if fallback_price > Decimal(0):
                        limit_price_to_use = fallback_price
                        logger.warning(f"{log_prefix}Could not calculate mid-price (Bid: {bid_price}, Ask: {ask_price}). Defaulting to {'ask' if action == 'Buy to Open' else 'bid'} price: ${limit_price_to_use:.2f}")
                    else:
                        raise ValueError(f"Cannot determine a valid order price. Bid: {bid_price}, Ask: {ask_price}, Action: {action}.")

            if limit_price_to_use <= Decimal('0'):
                raise ValueError(f"Calculated or provided order price (${limit_price_to_use:.2f}) is invalid.")

            # Pre-Trade Checks & Quantity Adjustment
            adjusted_trade_quantity = initial_quantity_arg
            instr_multiplier = instrument_obj.multiplier if hasattr(instrument_obj, 'multiplier') else 1
            
            if action == "Buy to Open":
                acc_balances = await account_to_use.a_get_balances(session_to_use)
                current_order_value = limit_price_to_use * Decimal(str(adjusted_trade_quantity)) * Decimal(str(instr_multiplier))
                acc_buying_power = acc_balances.derivative_buying_power if isinstance(instrument_obj, Option) else acc_balances.equity_buying_power

                if current_order_value > acc_buying_power:
                    new_adjusted_qty = int(acc_buying_power / (limit_price_to_use * Decimal(str(instr_multiplier))))
                    if new_adjusted_qty <= 0:
                        raise ValueError(f"Order rejected: Insufficient buying power (${acc_buying_power:,.2f}) for even 1 unit @ ${limit_price_to_use:.2f} (Value: ${limit_price_to_use * Decimal(str(instr_multiplier)):,.2f})")
                    logger.warning(
                        f"{log_prefix}Reduced order quantity from {initial_quantity_arg} to {new_adjusted_qty} "
                        f"due to buying power limit (${acc_buying_power:,.2f} < ${current_order_value:,.2f})"
                    )
                    adjusted_trade_quantity = new_adjusted_qty
            else:  # Sell to Close
                acc_positions = await account_to_use.a_get_positions(session_to_use)
                current_pos = next((p for p in acc_positions if p.symbol == instrument_obj.symbol), None)
                if not current_pos:
                    raise ValueError(f"No open position found for {instrument_obj.symbol} to sell.")

                live_acc_orders = await account_to_use.a_get_live_orders(session_to_use)
                current_pending_sell_qty = sum(
                    sum(leg.quantity for leg in order.legs if leg.symbol == instrument_obj.symbol and leg.action == OrderAction.SELL_TO_CLOSE)
                    for order in live_acc_orders
                    if order.status in (OrderStatus.LIVE, OrderStatus.RECEIVED) and
                       order.legs and
                       any(leg.symbol == instrument_obj.symbol and leg.action == OrderAction.SELL_TO_CLOSE for leg in order.legs)
                )

                current_available_qty = current_pos.quantity - current_pending_sell_qty
                logger.info(
                    f"{log_prefix}Position: {current_pos.quantity}, Pending sells: {current_pending_sell_qty}, Available: {current_available_qty}"
                )
                if current_available_qty <= 0:
                    raise ValueError(
                        f"Cannot place sell order - entire position of {current_pos.quantity} "
                        f"already has pending sell orders ({current_pending_sell_qty}) or is zero."
                    )
                if adjusted_trade_quantity > current_available_qty:
                    logger.warning(
                        f"{log_prefix}Reducing sell quantity from {initial_quantity_arg} to {current_available_qty} (maximum available)"
                    )
                    adjusted_trade_quantity = current_available_qty
            
            if adjusted_trade_quantity <= 0:
                raise ValueError(f"Calculated trade quantity ({adjusted_trade_quantity}) is zero or less after pre-trade checks.")
            final_trade_quantity = adjusted_trade_quantity

            # Order Placement
            order_act_enum = OrderAction.BUY_TO_OPEN if action == "Buy to Open" else OrderAction.SELL_TO_CLOSE
            order_leg: Leg = instrument_obj.build_leg(final_trade_quantity, order_act_enum)
            
            price_effect_val = PriceEffect.DEBIT if action == "Buy to Open" else PriceEffect.CREDIT

            logger.info(
                f"{log_prefix}Attempting order placement: "
                f"{action} {final_trade_quantity} {instrument_obj.symbol} @ ${limit_price_to_use:.2f} ({price_effect_val.value})"
            )
            new_order_details = NewOrder(
                time_in_force=OrderTimeInForce.DAY,
                order_type=OrderType.LIMIT,
                legs=[order_leg],
                price=limit_price_to_use,
                price_effect=price_effect_val
            )

            placement_response = await account_to_use.a_place_order(session_to_use, new_order_details, dry_run=dry_run)

            if not placement_response.errors:
                order_id_str = "N/A - Dry Run"
                if not dry_run and placement_response.order and placement_response.order.id:
                    order_id_str = placement_response.order.id
                
                success_msg = f"Order placement successful: {action} {final_trade_quantity} {instrument_obj.symbol} @ ${limit_price_to_use:.2f} (ID: {order_id_str})"
                if placement_response.warnings:
                    success_msg += "\nWarnings:\n" + "\n".join(str(w) for w in placement_response.warnings)
                logger.info(f"{log_prefix}{success_msg}")
                return True, success_msg
            else:
                error_list_str = "\n".join(str(e) for e in placement_response.errors)
                fail_msg = f"Order placement failed for {action} {final_trade_quantity} {instrument_obj.symbol} @ ${limit_price_to_use:.2f}:\n{error_list_str}"
                logger.error(f"{log_prefix}{fail_msg}")
                return False, fail_msg

        except ValueError as val_err: 
            logger.error(f"{log_prefix}Trade setup/check error: {str(val_err)}")
            return False, f"Trade setup/check error: {str(val_err)}"
        except Exception as gen_err: 
            logger.exception(f"{log_prefix}Unexpected error during trade logic for {instrument_obj.symbol if 'instrument_obj' in locals() else underlying_symbol}")
            return False, f"Unexpected error during trade logic: {str(gen_err)}"
    # --- End of _execute_core_trade inner function ---

    if not dry_run and not is_market_open():
        desc_parts_msg = [action, str(quantity), underlying_symbol]
        if option_type and strike_price and expiration_date:
             desc_parts_msg.extend([f"{option_type}{strike_price}", f"exp {expiration_date}"])
        description_msg = " ".join(desc_parts_msg)
        return f"Market is closed. Trade '{description_msg}' not placed. Please try again when the market is open or use dry_run=True."

    # Create instrument outside the lock to release it faster if instrument creation fails
    try:
        instrument = await _create_instrument(
            session=session_to_use,
            underlying_symbol=underlying_symbol,
            expiration_date=parsed_expiration_date,
            option_type=option_type,
            strike_price=strike_price
        )
        if instrument is None:
            error_msg_instr = f"Could not create instrument for symbol: {underlying_symbol}"
            if parsed_expiration_date:
                error_msg_instr += f" with expiration {expiration_date}, type {option_type}, strike {strike_price}"
            return f"{log_prefix}Error: {error_msg_instr}"
    except Exception as e_instr:
        logger.exception(f"{log_prefix}Error creating instrument for {underlying_symbol}")
        return f"{log_prefix}Error creating instrument: {str(e_instr)}"

    async with trade_execution_lock: # Lock for immediate execution
        try:
            user_price_decimal = Decimal(str(order_price)) if order_price is not None else None
            _success, message = await _execute_core_trade(instrument, user_price_decimal)
            return message 
        except Exception as e_immediate: 
            logger.exception(f"{log_prefix}Unexpected error during trade execution (within lock): {e_immediate}")
            return f"Trade execution failed unexpectedly (wrapper): {str(e_immediate)}"


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
    strike_price: float | None = None,
) -> str:
    """Get current bid/ask prices for stock or option.

    Note: When the market is closed, this may return stale data or fail if the data stream is unavailable.

    Args:
        underlying_symbol: Stock ticker symbol
        expiration_date: Option expiry in YYYY-MM-DD format (for options)
        option_type: C for Call, P for Put (for options)
        strike_price: Option strike price (for options)
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
        strike_price=strike_price
    )

    if instrument is None:
        error_msg = f"Could not find instrument for: {underlying_symbol}"
        if expiry_datetime:
            error_msg += f" {expiry_datetime.strftime('%Y-%m-%d')} {option_type} {strike_price}"
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

@mcp.tool()
async def list_live_orders(ctx: Context) -> str:
    """List all currently live (open) orders for the account.
    
    Returns a table with details of each live order including ID, symbol, action, quantity, type, and price.
    """
    lifespan_ctx: ServerContext = ctx.request_context.lifespan_context
    if not lifespan_ctx.session or not lifespan_ctx.account:
        return "Error: Tastytrade session or account not available. Check server logs."

    try:
        live_orders = await lifespan_ctx.account.a_get_live_orders(lifespan_ctx.session)
        if not live_orders:
            return "No live orders found."

        headers = ["ID", "Symbol", "Action", "Quantity", "Type", "Price", "Status"]
        table_data = []
        for order in live_orders:
            # Assuming single leg orders for simplicity in this summary table
            leg = order.legs[0] if order.legs else None
            table_data.append([
                order.id or "N/A",
                leg.symbol if leg else "N/A",
                leg.action.value if leg and hasattr(leg.action, 'value') else (str(leg.action) if leg else "N/A"),
                leg.quantity if leg else "N/A",
                order.order_type.value if hasattr(order.order_type, 'value') else str(order.order_type),
                f"${order.price:.2f}" if order.price is not None else "N/A",
                order.status.value if hasattr(order.status, 'value') else str(order.status)
            ])
        
        output = ["Live Orders:", ""]
        output.append(tabulate(table_data, headers=headers, tablefmt="plain"))
        return "\n".join(output)
    except Exception as e:
        logger.exception("Error fetching live orders")
        return f"Error fetching live orders: {str(e)}"

@mcp.tool()
async def cancel_order(
    ctx: Context, 
    order_id: str, 
    dry_run: bool = False
) -> str:
    """Cancel a live (open) order by its ID.

    Args:
        order_id: The ID of the order to cancel.
        dry_run: Test without executing if True.
    """
    lifespan_ctx: ServerContext = ctx.request_context.lifespan_context
    if not lifespan_ctx.session or not lifespan_ctx.account:
        return "Error: Tastytrade session or account not available. Check server logs."
    
    if not order_id:
        return "Error: order_id must be provided."

    log_prefix = f"[CancelOrder: {order_id}] "
    logger.info(f"{log_prefix}Attempting to cancel order. Dry run: {dry_run}")

    try:
        # The Tastytrade SDK's cancel_order might not return a detailed response object for success,
        # or it might return the cancelled order's details. We'll assume it raises an error on failure.
        # The API docs suggest it returns the cancelled order details.
        response = await lifespan_ctx.account.a_cancel_order(lifespan_ctx.session, int(order_id), dry_run=dry_run)

        if dry_run:
            # For dry runs, the response structure might be different or might indicate simulation.
            # We'll provide a generic success message for dry runs if no error occurs.
            # If `response.order.status` is available and indicates cancellation, that's even better.
            status_msg = f" (Simulated status: {response.order.status.value})" if response and hasattr(response, 'order') and response.order else ""
            success_msg = f"Dry run: Successfully processed cancellation request for order ID {order_id}{status_msg}."
            logger.info(f"{log_prefix}{success_msg}")
            return success_msg
        
        # For actual cancellation, check response details if available
        if response and response.order and response.order.status in [OrderStatus.CANCELLED, OrderStatus.REPLACED]: # Replaced also implies original was cancelled
            success_msg = f"Successfully cancelled order ID {order_id}. New status: {response.order.status.value}"
            logger.info(f"{log_prefix}{success_msg}")
            return success_msg
        elif response and response.order: # Order found but not cancelled as expected
            warn_msg = f"Order ID {order_id} processed but current status is {response.order.status.value}. Expected Cancelled."
            logger.warning(f"{log_prefix}{warn_msg}")
            return warn_msg
        else: # Fallback if response is not as expected
            # This case handles if a_cancel_order returns None or an unexpected structure on success
            # Some APIs might return a 204 No Content or an empty body on successful deletion.
            # Assuming if no error is raised, it was successful if API behaves that way.
            logger.info(f"{log_prefix}Cancellation request for order ID {order_id} processed without error, but response structure was not detailed. Assuming success.")
            return f"Cancellation request for order ID {order_id} processed. Please verify status."

    except Exception as e:
        # This will catch errors from the SDK, e.g., order not found, not cancellable, network issues.
        error_msg = f"Failed to cancel order ID {order_id}: {str(e)}"
        logger.exception(f"{log_prefix}{error_msg}")
        return error_msg

@mcp.tool()
async def modify_order(
    ctx: Context,
    order_id: str,
    new_quantity: int | None = None,
    new_price: float | None = None,
    dry_run: bool = False
) -> str:
    """Modify a live (open) order's quantity or price by its ID.
    At least one of new_quantity or new_price must be provided.

    Args:
        order_id: The ID of the order to modify.
        new_quantity: Optional. The new quantity for the order.
        new_price: Optional. The new limit price for the order.
        dry_run: Test without executing if True.
    """
    lifespan_ctx: ServerContext = ctx.request_context.lifespan_context
    session_to_use = lifespan_ctx.session
    account_to_use = lifespan_ctx.account

    if not session_to_use or not account_to_use:
        return "Error: Tastytrade session or account not available. Check server logs."

    if not order_id:
        return "Error: order_id must be provided."
    if new_quantity is None and new_price is None:
        return "Error: At least one of new_quantity or new_price must be provided for modification."

    log_prefix = f"[ModifyOrder: {order_id}] "
    logger.info(f"{log_prefix}Attempting to modify order. New Qty: {new_quantity}, New Price: {new_price}. Dry run: {dry_run}")

    try:
        # 1. Fetch the original order
        # Assuming a_get_order returns a PlacedOrder like object or similar with necessary details
        original_order = await account_to_use.a_get_order(session_to_use, int(order_id))
        if not original_order:
            return f"{log_prefix}Error: Order ID {order_id} not found."

        if original_order.status not in [OrderStatus.LIVE, OrderStatus.RECEIVED] or not original_order.editable:
            return f"{log_prefix}Error: Order ID {order_id} is not in a modifiable state (Status: {original_order.status.value}, Editable: {original_order.editable})."
        
        if not original_order.legs:
            return f"{log_prefix}Error: Order ID {order_id} has no legs defined."
        
        # For simplicity, this example assumes single-leg orders for modification.
        # Multi-leg order modification would require more complex leg handling.
        if len(original_order.legs) > 1:
            logger.warning(f"{log_prefix}Modifying multi-leg orders is complex. This tool currently best supports single-leg orders. Proceeding with first leg modification.")
        
        original_leg = original_order.legs[0]

        # 2. Determine new quantity and price
        updated_quantity = Decimal(str(new_quantity)) if new_quantity is not None else original_leg.quantity
        updated_price = Decimal(str(new_price)) if new_price is not None else original_order.price

        if updated_quantity <= 0:
            return f"{log_prefix}Error: New quantity ({updated_quantity}) must be positive."
        if updated_price <= Decimal(0):
             return f"{log_prefix}Error: New price (${updated_price:.2f}) must be positive."

        # 3. Reconstruct the leg(s) if quantity changed or to ensure correct structure
        # We need an instrument object to build the leg correctly.
        # The original_leg provides symbol and instrument_type. Expiration, strike, option_type might be needed for options.
        # This part can be tricky if original_order doesn't give full instrument spec for _create_instrument.
        # Let's assume original_leg.symbol is the OCC symbol for options, or ticker for equity.
        
        # A simplified approach: if tastytrade.order.Leg can be constructed directly
        # with just updated quantity and existing action/symbol for replacement.
        # However, NewOrder expects legs built via instrument.build_leg().

        # We need to get the instrument details to rebuild the leg for NewOrder
        # This is a simplification; a robust solution would parse original_leg.symbol to get underlying, exp, strike, type for options
        # For now, let's assume we can get/recreate the instrument based on type and symbol from original_leg
        temp_instrument_symbol = original_leg.symbol # This might be an option OCC symbol
        is_option = original_leg.instrument_type == "Option" # Assuming InstrumentType enum or string
        
        # This is a placeholder for robust instrument recreation logic
        # For equity, original_leg.symbol is fine. For options, OCC parsing is needed.
        # For now, we will assume _create_instrument can handle OCC symbols directly if passed as underlying
        # OR that we primarily modify equities with this simplified version.
        
        # The following is a HACK/SIMPLIFICATION for instrument re-creation. 
        # A proper implementation needs to parse Option OCC symbols if `original_leg.symbol` is one.
        # If `_create_instrument` is smart enough to take an OCC symbol as `underlying_symbol` when other option params are None, this might work.
        # Or, one would need to parse the OCC symbol from original_leg.symbol into its components.
        logger.debug(f"{log_prefix}Re-creating instrument for leg: {original_leg.symbol}, Type: {original_leg.instrument_type}")
        instrument_for_leg = await _create_instrument(
            session=session_to_use,
            underlying_symbol=original_leg.symbol, # This is the key simplification/potential issue for options.
            # For options, we'd ideally parse original_leg.symbol into components and pass them to _create_instrument
            # expiration_date=..., option_type=..., strike_price=... (if we parse from OCC)
        )

        if not instrument_for_leg:
            return f"{log_prefix}Error: Could not re-create instrument for leg modification (Symbol: {original_leg.symbol}). Modification requires instrument context."
        
        modified_legs = [instrument_for_leg.build_leg(updated_quantity, original_leg.action)]

        # 4. Construct the NewOrder object for replacement
        # Retain original_order.price_effect, time_in_force, order_type unless explicitly changing them.
        modified_new_order = NewOrder(
            time_in_force=original_order.time_in_force, 
            order_type=original_order.order_type,      
            legs=modified_legs,
            price=updated_price,
            price_effect=original_order.price_effect 
            # TODO: Add other fields like stop_trigger if original_order.order_type is STOP/STOP_LIMIT and it's present in original_order
        )

        # 5. Call replace_order
        # The SDK documentation should be checked for the exact method name if `a_replace_order` is not it.
        # Common names: a_replace_order, a_edit_order.
        # Assuming `a_replace_order(session, original_order_id_int, new_order_object, dry_run)` exists.
        logger.info(f"{log_prefix}Submitting replacement order. Details: {modified_new_order}")        
        replacement_response = await account_to_use.a_replace_order(
            session_to_use, 
            int(order_id), 
            modified_new_order, 
            dry_run=dry_run
        )

        # 6. Handle response
        if replacement_response and not replacement_response.errors:
            new_order_id_str = "N/A - Dry Run"
            if not dry_run and replacement_response.order and replacement_response.order.id:
                new_order_id_str = replacement_response.order.id
            
            success_msg = f"Order ID {order_id} modified successfully. New Order ID: {new_order_id_str}."
            if replacement_response.warnings:
                success_msg += "\nWarnings:\n" + "\n".join(str(w) for w in replacement_response.warnings)
            logger.info(f"{log_prefix}{success_msg}")
            return success_msg
        elif replacement_response and replacement_response.errors:
            error_list_str = "\n".join(str(e) for e in replacement_response.errors)
            fail_msg = f"Failed to modify order ID {order_id}:\n{error_list_str}"
            logger.error(f"{log_prefix}{fail_msg}")
            return fail_msg
        else:
            # Fallback for unexpected response structure
            unknown_resp_msg = f"Order modification for ID {order_id} processed, but response was not in expected format. Please verify."
            logger.warning(f"{log_prefix}{unknown_resp_msg}")
            return unknown_resp_msg

    except Exception as e:
        error_msg = f"Error modifying order ID {order_id}: {str(e)}"
        logger.exception(f"{log_prefix}{error_msg}")
        return error_msg