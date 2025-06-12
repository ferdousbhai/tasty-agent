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
from tastytrade.order import NewOrder, OrderStatus, OrderAction, OrderTimeInForce, OrderType
from tastytrade.streamer import DXLinkStreamer

logger = logging.getLogger(__name__)


@dataclass
class ServerContext:
    session: Session | None
    account: Account | None

@asynccontextmanager
async def lifespan(_server: FastMCP) -> AsyncIterator[ServerContext]:
    """Manages Tastytrade session lifecycle."""

    def get_credential(key: str, env_var: str) -> str | None:
        """Get credential from keyring with fallback to environment variable."""
        try:
            if credential := keyring.get_password("tastytrade", key):
                return credential
        except Exception:
            return os.getenv(env_var) #fallback to environment variable

    username = get_credential("username", "TASTYTRADE_USERNAME")
    password = get_credential("password", "TASTYTRADE_PASSWORD")
    account_id = get_credential("account_id", "TASTYTRADE_ACCOUNT_ID")

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
            logger.warning(f"Multiple accounts found. Using first account: {account.account_number}")
        else:
            logger.info(f"Using Tastytrade account: {account.account_number}")

    context = ServerContext(session=session, account=account)
    logger.info("TastyTrade MCP server is ready to handle requests")
    yield context

mcp = FastMCP("TastyTrade", lifespan=lifespan)


@mcp.tool()
async def get_account_balances(ctx: Context) -> str:
    """Get account cash balance, buying power, and net liquidating value."""
    context = ctx.request_context.lifespan_context
    balances = await context.account.a_get_balances(context.session)
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
    """Get all open stock and option positions with current values."""
    context = ctx.request_context.lifespan_context
    positions = await context.account.a_get_positions(context.session, include_marks=True)

    if not positions:
        return "No open positions found."

    table_data = [
        [
            pos.symbol,
            getattr(pos.instrument_type, 'value', str(pos.instrument_type)),
            float(pos.quantity),
            f"${float(pos.mark_price):,.2f}" if pos.mark_price else "N/A",
            f"${float(pos.mark_price * pos.quantity * pos.multiplier):,.2f}" if pos.mark_price else "N/A"
        ]
        for pos in positions
    ]

    return "Current Positions:\n\n" + tabulate(
        table_data,
        headers=["Symbol", "Type", "Quantity", "Mark Price", "Value"],
        tablefmt="plain"
    )

@mcp.tool()
async def get_live_orders(ctx: Context) -> str:
    """Get all live (open) orders for the account."""
    context = ctx.request_context.lifespan_context
    live_orders = await context.account.a_get_live_orders(context.session)

    if not live_orders:
        return "No live orders found."

    table_data = [
        [
            order.id or "N/A",
            order.legs[0].symbol if order.legs else "N/A",
            getattr(order.legs[0].action, 'value', str(order.legs[0].action)) if order.legs else "N/A",
            float(order.legs[0].quantity) if order.legs else "N/A",
            getattr(order.order_type, 'value', str(order.order_type)),
            f"${float(order.price):.2f}" if order.price else "N/A",
            getattr(order.status, 'value', str(order.status))
        ]
        for order in live_orders
    ]

    return "Live Orders:\n\n" + tabulate(
        table_data,
        headers=["ID", "Symbol", "Action", "Quantity", "Type", "Price", "Status"],
        tablefmt="plain"
    )


async def _create_instrument(
    session: Session,
    underlying_symbol: str,
    expiration_date: datetime | None = None,
    option_type: Literal["C", "P"] | None = None,
    strike_price: float | None = None,
) -> Option | Equity | None:
    """Create an instrument object for stock or option."""
    if not expiration_date or not option_type or not strike_price:
        return await Equity.a_get(session, underlying_symbol)

    if not (chains := await NestedOptionChain.a_get(session, underlying_symbol)):
        logger.error(f"No option chain found for {underlying_symbol}")
        return None
    option_chain = chains[0]

    exp_date = expiration_date.date()
    if not (expiration := next(
        (exp for exp in option_chain.expirations if exp.expiration_date == exp_date), None
    )):
        logger.error(f"No expiration found for date {exp_date} in chain for {underlying_symbol}")
        return None

    if not (strike_obj := next(
        (s for s in expiration.strikes if float(s.strike_price) == strike_price), None
    )):
        logger.error(f"No strike found for {strike_price} on {exp_date} in chain for {underlying_symbol}")
        return None

    option_symbol = strike_obj.call if option_type == "C" else strike_obj.put
    return await Option.a_get(session, option_symbol)

async def _get_quote(session: Session, streamer_symbol: str) -> tuple[Decimal, Decimal]:
    """Get current quote for a symbol via DXLinkStreamer."""
    try:
        async with DXLinkStreamer(session) as streamer:
            await streamer.subscribe(Quote, [streamer_symbol])
            quote = await asyncio.wait_for(streamer.get_event(Quote), timeout=10.0)
            return Decimal(str(quote.bid_price)), Decimal(str(quote.ask_price))
    except asyncio.TimeoutError:
        raise ValueError(f"Timed out waiting for quote data for {streamer_symbol}")
    except asyncio.CancelledError:
        raise ValueError(f"WebSocket connection interrupted for {streamer_symbol}")


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
    """Execute a stock/option trade."""
    context = ctx.request_context.lifespan_context

    exp_date = datetime.strptime(expiration_date, "%Y-%m-%d") if expiration_date else None

    instrument = await _create_instrument(
        context.session, underlying_symbol, exp_date, option_type, strike_price
    )
    if not instrument:
        raise ValueError(f"Could not create instrument for {underlying_symbol}")

    bid_price, ask_price = await _get_quote(context.session, instrument.streamer_symbol)

    user_price = Decimal(str(order_price)) if order_price else None

    if user_price is not None:
        if bid_price > Decimal(0) and ask_price > Decimal(0):
            if user_price < bid_price:
                logger.warning(f"Adjusted order price from ${user_price:.2f} to ${bid_price:.2f} (bid price)")
                limit_price = bid_price
            elif user_price > ask_price:
                logger.warning(f"Adjusted order price from ${user_price:.2f} to ${ask_price:.2f} (ask price)")
                limit_price = ask_price
            else:
                limit_price = user_price
        else:
            limit_price = user_price
        logger.info(f"Using {'adjusted' if limit_price != user_price else 'user-provided'} order price: ${limit_price:.2f}")
    else:
        if bid_price > Decimal(0) and ask_price > Decimal(0) and ask_price >= bid_price:
            limit_price = ((bid_price + ask_price) / 2).quantize(Decimal('0.01'))
            logger.info(f"Using mid-price: ${limit_price:.2f}")
        else:
            fallback_price = ask_price if action == "Buy to Open" else bid_price
            if fallback_price > Decimal(0):
                logger.warning(f"Using {'ask' if action == 'Buy to Open' else 'bid'} price: ${fallback_price:.2f}")
                limit_price = fallback_price
            else:
                raise ValueError(f"Cannot determine valid order price. Bid: {bid_price}, Ask: {ask_price}")

    order_action = OrderAction.BUY_TO_OPEN if action == "Buy to Open" else OrderAction.SELL_TO_CLOSE

    order = NewOrder(
        time_in_force=OrderTimeInForce.DAY,
        order_type=OrderType.LIMIT,
        legs=[instrument.build_leg(quantity, order_action)],
        price=limit_price
    )

    logger.info(f"Placing order: {action} {quantity} {instrument.symbol} @ ${limit_price:.2f}")
    response = await context.account.a_place_order(context.session, order, dry_run=dry_run)

    if response.errors:
        error_msg = "\n".join(str(e) for e in response.errors)
        return f"Order placement failed:\n{error_msg}"

    order_id = "N/A - Dry Run" if dry_run else (response.order.id if response.order else "Unknown")
    success_msg = f"Order placement successful: {action} {quantity} {instrument.symbol} @ ${limit_price:.2f} (ID: {order_id})"

    if response.warnings:
        success_msg += "\nWarnings:\n" + "\n".join(str(w) for w in response.warnings)

    return success_msg

@mcp.tool()
async def get_nlv_history(
    ctx: Context,
    time_back: Literal['1d', '1m', '3m', '6m', '1y', 'all'] = '1y'
) -> str:
    """Get Net Liquidating Value (NLV) history for the account."""
    context = ctx.request_context.lifespan_context
    history = await context.account.a_get_net_liquidating_value_history(context.session, time_back=time_back)
    if not history:
        return "No history data available for the selected time period."

    parsed_data = [
        (
            datetime.strptime(n.time[:10], "%Y-%m-%d").date(),
            n.time[:10],
            f"{float(n.total_open):,.2f}",
            f"{float(n.total_high):,.2f}",
            f"{float(n.total_low):,.2f}",
            f"{float(n.total_close):,.2f}"
        )
        for n in history
    ]

    parsed_data.sort(key=lambda item: item[0], reverse=True)

    table_data = [
        [formatted_date, open_str, high_str, low_str, close_str]
        for _, formatted_date, open_str, high_str, low_str, close_str in parsed_data
    ]

    return f"Net Liquidating Value History (Past {time_back}):\n\n" + tabulate(
        table_data,
        headers=["Date", "Open ($)", "High ($)", "Low ($)", "Close ($)"],
        tablefmt="plain"
    )

@mcp.tool()
async def get_transaction_history(
    ctx: Context,
    start_date: str | None = None
) -> str:
    """Get account transaction history from start_date (YYYY-MM-DD) or last 90 days."""
    date_obj = (date.today() - timedelta(days=90) if start_date is None
                else datetime.strptime(start_date, "%Y-%m-%d").date())

    context = ctx.request_context.lifespan_context
    transactions = await context.account.a_get_history(context.session, start_date=date_obj)
    if not transactions:
        return "No transactions found for the specified period."

    table_data = [
        [
            txn.transaction_date.strftime("%Y-%m-%d"),
            txn.transaction_sub_type or 'N/A',
            txn.description or 'N/A',
            f"${float(txn.net_value):,.2f}" if txn.net_value is not None else 'N/A'
        ]
        for txn in transactions
    ]

    return "Transaction History:\n\n" + tabulate(
        table_data,
        headers=["Date", "Sub Type", "Description", "Value"],
        tablefmt="plain"
    )

@mcp.tool()
async def get_metrics(
    ctx: Context,
    symbols: list[str]
) -> str:
    """Get market metrics for symbols (IV Rank, Beta, Liquidity, Earnings)."""
    if not symbols:
        raise ValueError("No symbols provided.")

    context = ctx.request_context.lifespan_context
    metrics_data = await metrics.a_get_market_metrics(context.session, symbols)
    if not metrics_data:
        return f"No metrics found for the specified symbols: {', '.join(symbols)}"

    table_data = []
    for m in metrics_data:
        try:
            iv_rank = f"{float(m.implied_volatility_index_rank) * 100:.1f}%" if m.implied_volatility_index_rank else "N/A"
            iv_percentile = f"{float(m.implied_volatility_percentile) * 100:.1f}%" if m.implied_volatility_percentile else "N/A"
            beta = f"{float(m.beta):.2f}" if m.beta else "N/A"

            earnings_info = "N/A"
            if hasattr(m, 'earnings') and m.earnings:
                expected = getattr(m.earnings, "expected_report_date", None)
                time_of_day = getattr(m.earnings, "time_of_day", None)
                if expected and time_of_day:
                    earnings_info = f"{expected} ({time_of_day})"

            table_data.append([
                m.symbol,
                iv_rank,
                iv_percentile,
                beta,
                m.liquidity_rating or "N/A",
                m.lendability or "N/A",
                earnings_info
            ])
        except Exception:
            logger.warning("Skipping metric for symbol due to processing error: %s", m.symbol, exc_info=True)

    return "Market Metrics:\n\n" + tabulate(
        table_data,
        headers=["Symbol", "IV Rank", "IV %ile", "Beta", "Liquidity", "Lendability", "Earnings"],
        tablefmt="plain"
    )

@mcp.tool()
async def get_prices(
    ctx: Context,
    underlying_symbol: str,
    expiration_date: str | None = None,
    option_type: Literal["C", "P"] | None = None,
    strike_price: float | None = None,
) -> str:
    """Get current bid/ask prices for stock or option."""
    exp_date = datetime.strptime(expiration_date, "%Y-%m-%d") if expiration_date else None

    context = ctx.request_context.lifespan_context
    instrument = await _create_instrument(
        context.session, underlying_symbol, exp_date, option_type, strike_price
    )

    if not instrument:
        error_msg = f"Could not find instrument for: {underlying_symbol}"
        if exp_date:
            error_msg += f" {exp_date.strftime('%Y-%m-%d')} {option_type} {strike_price}"
        raise ValueError(error_msg)

    if not instrument.streamer_symbol:
        raise ValueError(f"Could not get streamer symbol for {instrument.symbol}")

    bid, ask = await _get_quote(context.session, instrument.streamer_symbol)
    return f"Current prices for {instrument.symbol}:\nBid: ${bid:.2f}\nAsk: ${ask:.2f}"

@mcp.tool()
async def cancel_order(
    ctx: Context,
    order_id: str,
    dry_run: bool = False
) -> str:
    """Cancel a live order by ID."""
    if not order_id:
        raise ValueError("order_id must be provided.")

    context = ctx.request_context.lifespan_context
    logger.info(f"Attempting to cancel order. Dry run: {dry_run}")
    response = await context.account.a_cancel_order(context.session, int(order_id), dry_run=dry_run)

    if dry_run:
        status_msg = f" (Simulated status: {response.order.status.value})" if response and response.order else ""
        return f"Dry run: Successfully processed cancellation request for order ID {order_id}{status_msg}."

    if response and response.order and response.order.status in [OrderStatus.CANCELLED, OrderStatus.REPLACED]:
        return f"Successfully cancelled order ID {order_id}. New status: {response.order.status.value}"
    elif response and response.order:
        return f"Order ID {order_id} processed but current status is {response.order.status.value}. Expected Cancelled."
    else:
        return f"Cancellation request for order ID {order_id} processed. Please verify status."

@mcp.tool()
async def modify_order(
    ctx: Context,
    order_id: str,
    new_quantity: int | None = None,
    new_price: float | None = None,
    dry_run: bool = False
) -> str:
    """Modify a live order's quantity or price by ID."""
    if not order_id:
        raise ValueError("order_id must be provided.")
    if new_quantity is None and new_price is None:
        raise ValueError("At least one of new_quantity or new_price must be provided.")

    context = ctx.request_context.lifespan_context
    original_order = await context.account.a_get_order(context.session, int(order_id))
    if not original_order:
        raise ValueError(f"Order ID {order_id} not found.")

    if original_order.status not in [OrderStatus.LIVE, OrderStatus.RECEIVED] or not original_order.editable:
        raise ValueError(f"Order ID {order_id} is not modifiable (Status: {original_order.status.value}).")

    if not original_order.legs or len(original_order.legs) > 1:
        raise ValueError("Order has no legs or multi-leg modification not supported.")

    original_leg = original_order.legs[0]
    updated_quantity = Decimal(str(new_quantity)) if new_quantity is not None else original_leg.quantity
    updated_price = Decimal(str(new_price)) if new_price is not None else original_order.price

    if updated_quantity <= 0 or updated_price <= Decimal(0):
        raise ValueError("New quantity and price must be positive.")

    instrument = await _create_instrument(context.session, original_leg.symbol)
    if not instrument:
        raise ValueError(f"Could not recreate instrument for {original_leg.symbol}.")

    modified_order = NewOrder(
        time_in_force=original_order.time_in_force,
        order_type=original_order.order_type,
        legs=[instrument.build_leg(updated_quantity, original_leg.action)],
        price=updated_price
    )

    response = await context.account.a_replace_order(context.session, int(order_id), modified_order, dry_run=dry_run)

    if response.errors:
        error_msg = "\n".join(str(e) for e in response.errors)
        raise ValueError(f"Failed to modify order ID {order_id}:\n{error_msg}")

    new_order_id = "N/A - Dry Run" if dry_run else (response.order.id if response.order else "Unknown")
    success_msg = f"Order ID {order_id} modified successfully. New Order ID: {new_order_id}."

    if response.warnings:
        success_msg += "\nWarnings:\n" + "\n".join(str(w) for w in response.warnings)

    return success_msg

@mcp.tool()
async def wait(duration_seconds: int = 60) -> str:
    """Wait for a specified duration in seconds (1-60)."""
    duration = max(1, min(duration_seconds, 60))
    await asyncio.sleep(duration)
    return f"Waited for {duration} seconds"

@mcp.tool()
async def check_market_status() -> str:
    """Check if the market is currently open or closed."""
    nyse = get_calendar('XNYS')
    current_time = datetime.now(ZoneInfo('America/New_York'))

    if nyse.is_open_on_minute(current_time):
        return "Market is currently OPEN"

    next_open = nyse.next_open(current_time)
    time_until = next_open - current_time
    next_open_formatted = next_open.strftime("%A, %B %d, %Y at %I:%M %p %Z")

    return f"Market is currently CLOSED. Next market open: {next_open_formatted} (in {time_until})"