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
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP, Context
from exchange_calendars import get_calendar
from tastytrade import Session, Account, metrics
from tastytrade.dxfeed import Quote
from tastytrade.instruments import Option, Equity, NestedOptionChain
from tastytrade.order import NewOrder, OrderStatus, OrderAction, OrderTimeInForce, OrderType, Leg, PriceEffect
from tastytrade.streamer import DXLinkStreamer

logger = logging.getLogger(__name__)


@dataclass
class ServerContext:
    session: Session | None
    account: Account | None
    # Cached account data that will be exposed as MCP resources
    account_balances: dict | None = None
    current_positions: list | None = None
    live_orders: list | None = None
    last_update: datetime | None = None

async def _update_account_data(context: ServerContext):
    """Update cached account data."""
    if not context.session or not context.account:
        return

    try:
        # Get all account data concurrently
        balances_task = context.account.a_get_balances(context.session)
        positions_task = context.account.a_get_positions(context.session, include_marks=True)
        live_orders_task = context.account.a_get_live_orders(context.session)

        balances, positions, live_orders = await asyncio.gather(
            balances_task, positions_task, live_orders_task
        )

        # Update context
        context.account_balances = {
            "cash_balance": float(balances.cash_balance),
            "equity_buying_power": float(balances.equity_buying_power),
            "derivative_buying_power": float(balances.derivative_buying_power),
            "net_liquidating_value": float(balances.net_liquidating_value),
            "maintenance_excess": float(balances.maintenance_excess)
        }

        context.current_positions = [
            {
                "symbol": pos.symbol,
                "instrument_type": pos.instrument_type,
                "quantity": float(pos.quantity),
                "mark_price": float(pos.mark_price) if pos.mark_price else None,
                "multiplier": pos.multiplier,
                "value": float(pos.mark_price * pos.quantity * pos.multiplier) if pos.mark_price else None
            }
            for pos in positions
        ]

        context.live_orders = [
            {
                "id": order.id,
                "symbol": order.legs[0].symbol if order.legs else None,
                "action": order.legs[0].action.value if order.legs and hasattr(order.legs[0].action, 'value') else str(order.legs[0].action) if order.legs else None,
                "quantity": float(order.legs[0].quantity) if order.legs else None,
                "price": float(order.price) if order.price else None,
                "order_type": order.order_type.value if hasattr(order.order_type, 'value') else str(order.order_type),
                "status": order.status.value if hasattr(order.status, 'value') else str(order.status)
            }
            for order in live_orders
        ]

        context.last_update = datetime.now()
        logger.info("Updated account data cache")

    except Exception as e:
        logger.error(f"Failed to update account data: {e}")

@asynccontextmanager
async def lifespan(_server: FastMCP) -> AsyncIterator[ServerContext]:
    """Manages the trade state, lock, and Tastytrade session lifecycle."""
    # Get credentials from keyring or environment
    username = keyring.get_password("tastytrade", "username") or os.getenv("TASTYTRADE_USERNAME")
    password = keyring.get_password("tastytrade", "password") or os.getenv("TASTYTRADE_PASSWORD")
    account_id = keyring.get_password("tastytrade", "account_id") or os.getenv("TASTYTRADE_ACCOUNT_ID")

    if not username or not password:
        raise ValueError(
            "Missing Tastytrade credentials. Please run 'tasty-agent setup' or set "
            "TASTYTRADE_USERNAME and TASTYTRADE_PASSWORD environment variables."
        )

    # Create session and get accounts
    session = Session(username, password)
    accounts = Account.get(session)

    # Select account
    if account_id:
        account = next((acc for acc in accounts if acc.account_number == account_id), None)
        if not account:
            raise ValueError(f"Specified Tastytrade account ID '{account_id}' not found.")
    else:
        account = accounts[0]
        if len(accounts) > 1:
            logger.warning(f"Multiple accounts found. Using first account: {account.account_number}")
        else:
            logger.info(f"Using Tastytrade account: {account.account_number}")

    # Create and yield context
    context = ServerContext(
        session=session,
        account=account,
    )

    # Initial data load
    await _update_account_data(context)

    yield context

mcp = FastMCP("TastyTrade", lifespan=lifespan)

# Add MCP resources
@mcp.resource("account://balances")
async def get_account_balances_resource(ctx: Context) -> str:
    """Current account cash balance, buying power, and net liquidating value."""
    context: ServerContext = ctx.request_context.lifespan_context

    # Update data if stale (older than 30 seconds)
    if not context.last_update or (datetime.now() - context.last_update).seconds > 30:
        await _update_account_data(context)

    if not context.account_balances:
        return "Account balances unavailable"

    balances = context.account_balances
    return (
        f"Account Balances:\n"
        f"Cash Balance: ${balances['cash_balance']:,.2f}\n"
        f"Equity Buying Power: ${balances['equity_buying_power']:,.2f}\n"
        f"Derivative Buying Power: ${balances['derivative_buying_power']:,.2f}\n"
        f"Net Liquidating Value: ${balances['net_liquidating_value']:,.2f}\n"
        f"Maintenance Excess: ${balances['maintenance_excess']:,.2f}"
    )

@mcp.resource("account://positions")
async def get_current_positions_resource(ctx: Context) -> str:
    """All currently open stock and option positions with current values."""
    context: ServerContext = ctx.request_context.lifespan_context

    # Update data if stale
    if not context.last_update or (datetime.now() - context.last_update).seconds > 30:
        await _update_account_data(context)

    if not context.current_positions:
        return "No open positions found."

    headers = ["Symbol", "Type", "Quantity", "Mark Price", "Value"]
    table_data = []

    for pos in context.current_positions:
        table_data.append([
            pos["symbol"],
            pos["instrument_type"],
            pos["quantity"],
            f"${pos['mark_price']:,.2f}" if pos["mark_price"] else "N/A",
            f"${pos['value']:,.2f}" if pos["value"] else "N/A"
        ])

    output = ["Current Positions:", ""]
    output.append(tabulate(table_data, headers=headers, tablefmt="plain"))
    return "\n".join(output)

@mcp.resource("account://live-orders")
async def get_live_orders_resource(ctx: Context) -> str:
    """All currently live (open) orders for the account."""
    context: ServerContext = ctx.request_context.lifespan_context

    # Update data if stale
    if not context.last_update or (datetime.now() - context.last_update).seconds > 30:
        await _update_account_data(context)

    if not context.live_orders:
        return "No live orders found."

    headers = ["ID", "Symbol", "Action", "Quantity", "Type", "Price", "Status"]
    table_data = []

    for order in context.live_orders:
        table_data.append([
            order["id"] or "N/A",
            order["symbol"] or "N/A",
            order["action"] or "N/A",
            order["quantity"] or "N/A",
            order["order_type"],
            f"${order['price']:.2f}" if order["price"] else "N/A",
            order["status"]
        ])

    output = ["Live Orders:", ""]
    output.append(tabulate(table_data, headers=headers, tablefmt="plain"))
    return "\n".join(output)

# --- Helper Functions ---
def _get_market_status() -> tuple[bool, datetime | None]:
    """Get market status and next open time if market is closed."""
    nyse = get_calendar('XNYS')  # NYSE calendar
    current_time = datetime.now(ZoneInfo('America/New_York'))
    is_open = nyse.is_open_on_minute(current_time)

    if is_open:
        return True, None

    next_open = nyse.next_open(current_time)
    return False, next_open

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

    option_symbol = strike_obj.call if option_type == "C" else strike_obj.put
    return await Option.a_get(session, option_symbol)

async def _get_quote(session: Session, streamer_symbol: str) -> tuple[Decimal, Decimal]:
    """(Helper) Get current quote for a symbol via DXLinkStreamer."""
    try:
        async with DXLinkStreamer(session) as streamer:
            await streamer.subscribe(Quote, [streamer_symbol])
            quote = await asyncio.wait_for(streamer.get_event(Quote), timeout=10.0)
            return Decimal(str(quote.bid_price)), Decimal(str(quote.ask_price))
    except asyncio.TimeoutError:
        raise ValueError(f"Timed out waiting for quote data for {streamer_symbol}")
    except asyncio.CancelledError:
        raise ValueError(f"WebSocket connection interrupted for {streamer_symbol}")

async def _determine_limit_price(
    session: Session,
    instrument: Option | Equity,
    action: str,
    user_price: Decimal | None
) -> Decimal:
    """Determine the limit price for an order, adjusting user price if necessary."""
    bid_price, ask_price = await _get_quote(session, instrument.streamer_symbol)

    if user_price is not None:
        # Adjust user price to be within bid-ask range if necessary
        if bid_price > Decimal(0) and ask_price > Decimal(0):
            if user_price < bid_price:
                logger.warning(f"Adjusted order price from ${user_price:.2f} to ${bid_price:.2f} (bid price)")
                return bid_price
            elif user_price > ask_price:
                logger.warning(f"Adjusted order price from ${user_price:.2f} to ${ask_price:.2f} (ask price)")
                return ask_price
        logger.info(f"Using user-provided order price: ${user_price:.2f}")
        return user_price

    # Calculate mid-price when no user price provided
    if bid_price > Decimal(0) and ask_price > Decimal(0) and ask_price >= bid_price:
        mid_price = ((bid_price + ask_price) / 2).quantize(Decimal('0.01'))
        logger.info(f"Using mid-price: ${mid_price:.2f}")
        return mid_price

    # Fallback to bid/ask based on action
    fallback_price = ask_price if action == "Buy to Open" else bid_price
    if fallback_price > Decimal(0):
        logger.warning(f"Using {'ask' if action == 'Buy to Open' else 'bid'} price: ${fallback_price:.2f}")
        return fallback_price

    raise ValueError(f"Cannot determine valid order price. Bid: {bid_price}, Ask: {ask_price}")

async def _place_order(
    account: Account,
    session: Session,
    instrument: Option | Equity,
    action: str,
    quantity: int,
    limit_price: Decimal,
    dry_run: bool,
    context: ServerContext | None = None
) -> str:
    """Place the actual order and return result message."""
    order_action = OrderAction.BUY_TO_OPEN if action == "Buy to Open" else OrderAction.SELL_TO_CLOSE
    price_effect = PriceEffect.DEBIT if action == "Buy to Open" else PriceEffect.CREDIT

    order = NewOrder(
        time_in_force=OrderTimeInForce.DAY,
        order_type=OrderType.LIMIT,
        legs=[instrument.build_leg(quantity, order_action)],
        price=limit_price,
        price_effect=price_effect
    )

    logger.info(f"Placing order: {action} {quantity} {instrument.symbol} @ ${limit_price:.2f}")
    response = await account.a_place_order(session, order, dry_run=dry_run)

    if response.errors:
        error_msg = "\n".join(str(e) for e in response.errors)
        return f"Order placement failed:\n{error_msg}"

    # If order was successful and not a dry run, update account data cache
    if not dry_run and response.order and context:
        await _update_account_data(context)

    order_id = "N/A - Dry Run" if dry_run else (response.order.id if response.order else "Unknown")
    success_msg = f"Order placement successful: {action} {quantity} {instrument.symbol} @ ${limit_price:.2f} (ID: {order_id})"

    if response.warnings:
        success_msg += "\nWarnings:\n" + "\n".join(str(w) for w in response.warnings)

    return success_msg

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
    """Execute a stock/option trade.

    Args:
        action: Buy to Open or Sell to Close
        quantity: Number of shares/contracts
        underlying_symbol: Stock ticker symbol
        strike_price: Option strike price (if option)
        option_type: C for Call, P for Put (if option)
        expiration_date: Option expiry in YYYY-MM-DD format (if option)
        order_price: Optional limit price (defaults to mid-price)
        dry_run: Test without executing if True
    """
    ctx_data: ServerContext = ctx.request_context.lifespan_context
    if not ctx_data.session or not ctx_data.account:
        raise ValueError("Tastytrade session not available. Check server logs.")

    # Check market status for non-dry runs
    if not dry_run and not _get_market_status()[0]:
        desc = f"{action} {quantity} {underlying_symbol}"
        if option_type and strike_price and expiration_date:
            desc += f" {option_type}{strike_price} exp {expiration_date}"
        raise ValueError(f"Market is closed. Trade '{desc}' not placed. Use dry_run=True to test.")

    # Parse expiration date if provided
    exp_date = datetime.strptime(expiration_date, "%Y-%m-%d") if expiration_date else None

    # Create instrument
    instrument = await _create_instrument(
        ctx_data.session, underlying_symbol, exp_date, option_type, strike_price
    )
    if not instrument:
        raise ValueError(f"Could not create instrument for {underlying_symbol}")

    # Determine limit price
    user_price = Decimal(str(order_price)) if order_price else None
    limit_price = await _determine_limit_price(ctx_data.session, instrument, action, user_price)

    # Place order directly with the requested quantity
    return await _place_order(ctx_data.account, ctx_data.session, instrument, action, quantity, limit_price, dry_run, ctx_data)

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
        raise ValueError("Tastytrade session not available. Check server logs.")

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
            raise ValueError("Invalid date format. Please use YYYY-MM-DD (e.g., '2024-01-01')")

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
        raise ValueError("Input 'symbols' must be a list of strings.")

    if not symbols:
        raise ValueError("No symbols provided.")

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
            raise ValueError("Invalid expiration date format. Please use YYYY-MM-DD format")

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
        raise ValueError(error_msg)

    streamer_symbol = instrument.streamer_symbol
    if not streamer_symbol:
        raise ValueError(f"Could not get streamer symbol for {instrument.symbol}")

    bid, ask = await _get_quote(session, streamer_symbol)
    return (
        f"Current prices for {instrument.symbol}:\n"
        f"Bid: ${bid:.2f}\n"
        f"Ask: ${ask:.2f}"
    )

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
        raise ValueError("Tastytrade session or account not available. Check server logs.")

    if not order_id:
        raise ValueError("order_id must be provided.")

    logger.info(f"Attempting to cancel order. Dry run: {dry_run}")

    response = await lifespan_ctx.account.a_cancel_order(lifespan_ctx.session, int(order_id), dry_run=dry_run)

    if dry_run:
        status_msg = f" (Simulated status: {response.order.status.value})" if response and hasattr(response, 'order') and response.order else ""
        success_msg = f"Dry run: Successfully processed cancellation request for order ID {order_id}{status_msg}."
        logger.info(f"{success_msg}")
        return success_msg

    # For actual cancellation, check response details if available
    if response and response.order and response.order.status in [OrderStatus.CANCELLED, OrderStatus.REPLACED]:
        # Update cache after successful cancellation
        await _update_account_data(lifespan_ctx)
        success_msg = f"Successfully cancelled order ID {order_id}. New status: {response.order.status.value}"
        logger.info(f"{success_msg}")
        return success_msg
    elif response and response.order:
        warn_msg = f"Order ID {order_id} processed but current status is {response.order.status.value}. Expected Cancelled."
        logger.warning(f"{warn_msg}")
        return warn_msg
    else:
        # Update cache anyway in case the cancellation was successful
        await _update_account_data(lifespan_ctx)
        logger.info(f"Cancellation request for order ID {order_id} processed without error, but response structure was not detailed. Assuming success.")
        return f"Cancellation request for order ID {order_id} processed. Please verify status."

@mcp.tool()
async def modify_order(
    ctx: Context,
    order_id: str,
    new_quantity: int | None = None,
    new_price: float | None = None,
    dry_run: bool = False
) -> str:
    """Modify a live order's quantity or price by ID.

    Args:
        order_id: The ID of the order to modify
        new_quantity: New quantity for the order
        new_price: New limit price for the order
        dry_run: Test without executing if True
    """
    context: ServerContext = ctx.request_context.lifespan_context

    if not context.session or not context.account:
        raise ValueError("Tastytrade session not available. Check server logs.")

    if not order_id:
        raise ValueError("order_id must be provided.")
    if new_quantity is None and new_price is None:
        raise ValueError("At least one of new_quantity or new_price must be provided.")

    original_order = await context.account.a_get_order(context.session, int(order_id))
    if not original_order:
        raise ValueError(f"Order ID {order_id} not found.")

    if original_order.status not in [OrderStatus.LIVE, OrderStatus.RECEIVED] or not original_order.editable:
        raise ValueError(f"Order ID {order_id} is not modifiable (Status: {original_order.status.value}).")

    if not original_order.legs:
        raise ValueError(f"Order ID {order_id} has no legs defined.")

    if len(original_order.legs) > 1:
        raise ValueError("Multi-leg order modification not supported.")

    original_leg = original_order.legs[0]
    updated_quantity = Decimal(str(new_quantity)) if new_quantity is not None else original_leg.quantity
    updated_price = Decimal(str(new_price)) if new_price is not None else original_order.price

    if updated_quantity <= 0:
        raise ValueError("New quantity must be positive.")
    if updated_price <= Decimal(0):
        raise ValueError("New price must be positive.")

    instrument = await _create_instrument(context.session, original_leg.symbol)
    if not instrument:
        raise ValueError(f"Could not recreate instrument for {original_leg.symbol}.")

    modified_order = NewOrder(
        time_in_force=original_order.time_in_force,
        order_type=original_order.order_type,
        legs=[instrument.build_leg(updated_quantity, original_leg.action)],
        price=updated_price,
        price_effect=original_order.price_effect
    )

    response = await context.account.a_replace_order(
        context.session,
        int(order_id),
        modified_order,
        dry_run=dry_run
    )

    if response.errors:
        error_msg = "\n".join(str(e) for e in response.errors)
        raise ValueError(f"Failed to modify order ID {order_id}:\n{error_msg}")

    if not dry_run and response.order:
        await _update_account_data(context)

    new_order_id = "N/A - Dry Run" if dry_run else (response.order.id if response.order else "Unknown")
    success_msg = f"Order ID {order_id} modified successfully. New Order ID: {new_order_id}."

    if response.warnings:
        success_msg += "\nWarnings:\n" + "\n".join(str(w) for w in response.warnings)

    return success_msg

@mcp.tool()
async def check_market_status(ctx: Context) -> str:
    """Check if the market is currently open or closed.

    Returns the current market status and, if closed, when the market will next open.
    """
    is_open, next_open = _get_market_status()

    if is_open:
        return "Market is currently OPEN"
    else:
        current_time = datetime.now(ZoneInfo('America/New_York'))
        time_until = next_open - current_time
        next_open_formatted = next_open.strftime("%A, %B %d, %Y at %I:%M %p %Z")

        return (
            f"Market is currently CLOSED\n"
            f"Next market open: {next_open_formatted}\n"
            f"Time until market opens: {time_until}"
        )