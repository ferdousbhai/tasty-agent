import asyncio
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import humanize
from aiocache import Cache, cached
from aiocache.serializers import PickleSerializer
from aiolimiter import AsyncLimiter
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.prompts import base
from pydantic import BaseModel, Field
from tabulate import tabulate
from tastytrade import Account, Session
from tastytrade.dxfeed import Greeks, Quote
from tastytrade.instruments import Equity, Option, a_get_option_chain
from tastytrade.market_sessions import ExchangeType, MarketStatus, a_get_market_holidays, a_get_market_sessions
from tastytrade.metrics import a_get_market_metrics
from tastytrade.order import InstrumentType, NewOrder, OrderAction, OrderTimeInForce, OrderType
from tastytrade.search import a_symbol_search
from tastytrade.streamer import DXLinkStreamer
from tastytrade.utils import now_in_new_york
from tastytrade.watchlists import PrivateWatchlist, PublicWatchlist

logger = logging.getLogger(__name__)

rate_limiter = AsyncLimiter(2, 1) # 2 requests per second


async def _stream_events(
    session: Session,
    event_type: type[Quote] | type[Greeks],
    streamer_symbols: list[str],
    timeout: float
) -> list[Any]:
    """Generic streaming helper for Quote/Greeks events."""
    async with DXLinkStreamer(session) as streamer:
        await streamer.subscribe(event_type, streamer_symbols)
        events_by_symbol: dict[str, Any] = {}
        expected = set(streamer_symbols)
        while len(events_by_symbol) < len(expected):
            event = await asyncio.wait_for(streamer.get_event(event_type), timeout=timeout)
            if event.event_symbol in expected:
                events_by_symbol[event.event_symbol] = event
        return [events_by_symbol[s] for s in streamer_symbols]


async def _paginate[T](
    fetch_fn: Callable[[int], Awaitable[list[T]]],
    page_size: int
) -> list[T]:
    """Generic pagination helper for API calls."""
    all_items: list[T] = []
    offset = 0
    while True:
        async with rate_limiter:
            items = await fetch_fn(offset)
        all_items.extend(items or [])
        if not items or len(items) < page_size:
            break
        offset += page_size
    return all_items


def to_table(data: Sequence[BaseModel]) -> str:
    """Format list of Pydantic models as a plain table."""
    if not data:
        return "No data"
    return tabulate([item.model_dump() for item in data], headers='keys', tablefmt='plain')

@dataclass
class ServerContext:
    session: Session
    account: Account


def get_context(ctx: Context) -> ServerContext:
    """Extract context from request."""
    return ctx.request_context.lifespan_context


def get_valid_session(ctx: Context) -> Session:
    """Get a valid session, refreshing if expired or about to expire.

    TastyTrade sessions expire after 15 minutes. This function checks the
    expiration time and refreshes proactively if within 5 seconds of expiry.
    """
    context = get_context(ctx)
    session = context.session

    # Refresh if expired or expiring within 5 seconds (buffer for network latency)
    time_until_expiry = (session.session_expiration - now_in_new_york()).total_seconds()
    if time_until_expiry < 5:
        logger.info(f"Session expiring in {time_until_expiry:.0f}s, refreshing...")
        session.refresh()
        logger.info(f"Session refreshed, new expiration: {session.session_expiration}")

    return session

@asynccontextmanager
async def lifespan(_) -> AsyncIterator[ServerContext]:
    """Manages Tastytrade session lifecycle."""

    client_secret = os.getenv("TASTYTRADE_CLIENT_SECRET")
    refresh_token = os.getenv("TASTYTRADE_REFRESH_TOKEN")
    account_id = os.getenv("TASTYTRADE_ACCOUNT_ID")

    if not client_secret or not refresh_token:
        logger.error("Missing Tastytrade OAuth credentials. Set TASTYTRADE_CLIENT_SECRET and TASTYTRADE_REFRESH_TOKEN environment variables.")
        raise ValueError(
            "Missing Tastytrade OAuth credentials. Set TASTYTRADE_CLIENT_SECRET and "
            "TASTYTRADE_REFRESH_TOKEN environment variables."
        )

    try:
        session = Session(client_secret, refresh_token)
        accounts = Account.get(session)
        logger.info(f"Successfully authenticated with Tastytrade. Found {len(accounts)} account(s).")
    except Exception as e:
        logger.error(f"Failed to authenticate with Tastytrade: {e}")
        raise

    if account_id:
        account = next((acc for acc in accounts if acc.account_number == account_id), None)
        if not account:
            logger.error(f"Account '{account_id}' not found in available accounts: {[acc.account_number for acc in accounts]}")
            raise ValueError(f"Account '{account_id}' not found.")
        logger.info(f"Using specified account: {account.account_number}")
    else:
        account = accounts[0]
        logger.info(f"Using default account: {account.account_number}")

    yield ServerContext(
        session=session,
        account=account
    )

mcp_app = FastMCP("TastyTrade", lifespan=lifespan)

# =============================================================================
# ACCOUNT & POSITION TOOLS
# =============================================================================

@mcp_app.tool()
async def get_balances(ctx: Context) -> dict[str, Any]:
    context = get_context(ctx)
    session = get_valid_session(ctx)
    return {k: v for k, v in (await context.account.a_get_balances(session)).model_dump().items() if v is not None and v != 0}


@mcp_app.tool()
async def get_positions(ctx: Context) -> str:
    context = get_context(ctx)
    session = get_valid_session(ctx)
    positions = await context.account.a_get_positions(session, include_marks=True)
    return to_table(positions)


@mcp_app.tool()
async def get_net_liquidating_value_history(
    ctx: Context,
    time_back: Literal['1d', '1m', '3m', '6m', '1y', 'all'] = '1y'
) -> str:
    """Portfolio value over time. ⚠️ Use with get_transaction_history(transaction_type="Money Movement") to separate trading performance from deposits/withdrawals."""
    context = get_context(ctx)
    session = get_valid_session(ctx)
    history = await context.account.a_get_net_liquidating_value_history(session, time_back=time_back)
    return to_table(history)



# =============================================================================
# MARKET DATA TOOLS
# =============================================================================

@dataclass
class InstrumentDetail:
    """Details for a resolved instrument."""
    streamer_symbol: str
    instrument: Equity | Option


class InstrumentSpec(BaseModel):
    """Specification for an instrument (stock or option)."""
    symbol: str = Field(..., description="Stock symbol (e.g., 'AAPL', 'TQQQ')")
    option_type: Literal['C', 'P'] | None = Field(None, description="Option type: 'C' for call, 'P' for put (omit for stocks)")
    strike_price: float | None = Field(None, description="Strike price (required for options)")
    expiration_date: str | None = Field(None, description="Expiration date in YYYY-MM-DD format (required for options)")


class OrderLeg(BaseModel):
    """Specification for an order leg."""
    symbol: str = Field(..., description="Stock symbol (e.g., 'TQQQ', 'AAPL')")
    action: str = Field(..., description="For stocks: 'Buy' or 'Sell'. For options: 'Buy to Open', 'Buy to Close', 'Sell to Open', 'Sell to Close'")
    quantity: int = Field(..., description="Number of contracts/shares")
    option_type: Literal['C', 'P'] | None = Field(None, description="Option type: 'C' for call, 'P' for put (omit for stocks)")
    strike_price: float | None = Field(None, description="Strike price (required for options)")
    expiration_date: str | None = Field(None, description="Expiration date in YYYY-MM-DD format (required for options)")


class WatchlistSymbol(BaseModel):
    """Symbol specification for watchlist operations."""
    symbol: str = Field(..., description="Stock symbol (e.g., 'AAPL', 'TSLA')")
    instrument_type: str = Field(..., description="One of: 'Equity', 'Equity Option', 'Future', 'Future Option', 'Cryptocurrency', 'Warrant'")


def validate_date_format(date_string: str) -> date:
    """Validate date format and return date object."""
    try:
        return datetime.strptime(date_string, "%Y-%m-%d").date()
    except ValueError as e:
        raise ValueError(f"Invalid date format '{date_string}'. Expected YYYY-MM-DD format.") from e


def validate_strike_price(strike_price: Any) -> float:
    """Validate and convert strike price to float."""
    try:
        strike = float(strike_price)
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid strike price '{strike_price}'. Expected positive number.") from e
    if strike <= 0:
        raise ValueError(f"Invalid strike price '{strike_price}'. Must be positive.")
    return strike


def _option_chain_key_builder(fn, session: Session, symbol: str):
    """Build cache key using only symbol (session changes but symbol is stable)."""
    return f"option_chain:{symbol}"


@cached(ttl=86400, cache=Cache.MEMORY, serializer=PickleSerializer(), key_builder=_option_chain_key_builder)
async def get_cached_option_chain(session: Session, symbol: str):
    """Cache option chains for 24 hours as they rarely change during that timeframe."""
    return await a_get_option_chain(session, symbol)


async def get_instrument_details(session: Session, instrument_specs: list[InstrumentSpec]) -> list[InstrumentDetail]:
    """Get instrument details with validation and caching."""
    async def lookup_single_instrument(spec: InstrumentSpec) -> InstrumentDetail:
        symbol = spec.symbol.upper()
        option_type = spec.option_type

        if option_type:
            # Validate option parameters
            strike_price = validate_strike_price(spec.strike_price)
            expiration_date = spec.expiration_date
            if not expiration_date:
                raise ValueError(f"expiration_date is required for option {symbol}")

            target_date = validate_date_format(expiration_date)
            option_type = option_type.upper()
            if option_type not in ['C', 'P']:
                raise ValueError(f"Invalid option_type '{option_type}'. Expected 'C' or 'P'.")

            # Get cached option chain and find specific option
            chain = await get_cached_option_chain(session, symbol)
            if target_date not in chain:
                available_dates = sorted(chain.keys())
                raise ValueError(f"No options found for {symbol} expiration {expiration_date}. Available: {available_dates}")

            for option in chain[target_date]:
                if (option.strike_price == strike_price and
                    option.option_type.value == option_type):
                    return InstrumentDetail(option.streamer_symbol, option)

            available_strikes = [opt.strike_price for opt in chain[target_date] if opt.option_type.value == option_type]
            raise ValueError(f"Option not found: {symbol} {expiration_date} {option_type} {strike_price}. Available strikes: {sorted(set(available_strikes))}")
        else:
            # Get equity instrument
            instrument = await Equity.a_get(session, symbol)
            return InstrumentDetail(symbol, instrument)

    return await asyncio.gather(*[lookup_single_instrument(spec) for spec in instrument_specs])


@mcp_app.tool()
async def get_quotes(
    ctx: Context,
    instruments: list[InstrumentSpec],
    timeout: float = 10.0
) -> str:
    """
    Get live quotes for multiple stocks and/or options.

    Args:
        instruments: List of instrument specifications. Each contains:
            - symbol: str - Stock symbol (e.g., 'AAPL', 'TQQQ')
            - option_type: 'C' or 'P' (optional, omit for stocks)
            - strike_price: float (required for options)
            - expiration_date: str - YYYY-MM-DD format (required for options)
        timeout: Timeout in seconds

    Examples:
        Single stock: get_quotes([{"symbol": "AAPL"}])
        Single option: get_quotes([{"symbol": "TQQQ", "option_type": "C", "strike_price": 100.0, "expiration_date": "2026-01-16"}])
        Multiple instruments: get_quotes([
            {"symbol": "AAPL"},
            {"symbol": "AAPL", "option_type": "C", "strike_price": 150.0, "expiration_date": "2024-12-20"},
            {"symbol": "AAPL", "option_type": "C", "strike_price": 155.0, "expiration_date": "2024-12-20"}
        ])
    """
    if not instruments:
        logger.error("get_quotes called with empty instruments list")
        raise ValueError("At least one instrument is required")

    session = get_valid_session(ctx)
    instrument_details = await get_instrument_details(session, instruments)

    try:
        quotes = await _stream_events(session, Quote, [d.streamer_symbol for d in instrument_details], timeout)
        return to_table(quotes)
    except TimeoutError as e:
        logger.warning(f"Timeout getting quotes for {len(instruments)} instruments after {timeout}s")
        raise ValueError(f"Timeout getting quotes after {timeout}s") from e
    except Exception as e:
        logger.error(f"Error getting quotes for instruments {[i.symbol for i in instruments]}: {e}")
        raise ValueError(f"Error getting quotes: {e}") from e


@mcp_app.tool()
async def get_greeks(
    ctx: Context,
    options: list[InstrumentSpec],
    timeout: float = 10.0
) -> str:
    """
    Get Greeks (delta, gamma, theta, vega, rho) for multiple options.

    Args:
        options: List of option specifications. Each contains:
            - symbol: str - Stock symbol (e.g., 'AAPL', 'TQQQ')
            - option_type: 'C' or 'P'
            - strike_price: float - Strike price of the option
            - expiration_date: str - Expiration date in YYYY-MM-DD format
        timeout: Timeout in seconds

    Examples:
        Single option: get_greeks([{"symbol": "TQQQ", "option_type": "C", "strike_price": 100.0, "expiration_date": "2026-01-16"}])
        Multiple options: get_greeks([
            {"symbol": "AAPL", "option_type": "C", "strike_price": 150.0, "expiration_date": "2024-12-20"},
            {"symbol": "AAPL", "option_type": "P", "strike_price": 150.0, "expiration_date": "2024-12-20"}
        ])
    """
    if not options:
        logger.error("get_greeks called with empty options list")
        raise ValueError("At least one option is required")

    session = get_valid_session(ctx)
    option_details = await get_instrument_details(session, options)

    try:
        greeks = await _stream_events(session, Greeks, [d.streamer_symbol for d in option_details], timeout)
        return to_table(greeks)
    except TimeoutError as e:
        logger.warning(f"Timeout getting Greeks for {len(options)} options after {timeout}s")
        raise ValueError(f"Timeout getting Greeks after {timeout}s") from e
    except Exception as e:
        logger.error(f"Error getting Greeks for options {[opt.symbol for opt in options]}: {e}")
        raise ValueError(f"Error getting Greeks: {e}") from e


# =============================================================================
# HISTORY TOOLS
# =============================================================================

@mcp_app.tool()
async def get_transaction_history(
    ctx: Context,
    days: int = 90,
    underlying_symbol: str | None = None,
    transaction_type: Literal["Trade", "Money Movement"] | None = None
) -> str:
    """Get account transaction history including trades and money movements for the last N days (default: 90)."""
    context = get_context(ctx)
    session = get_valid_session(ctx)
    start = date.today() - timedelta(days=days)

    trades = await _paginate(
        lambda offset: context.account.a_get_history(
            session, start_date=start, underlying_symbol=underlying_symbol,
            type=transaction_type, per_page=250, page_offset=offset
        ),
        page_size=250
    )
    return to_table(trades)


@mcp_app.tool()
async def get_order_history(
    ctx: Context,
    days: int = 7,
    underlying_symbol: str | None = None
) -> str:
    """Get all order history for the last N days (default: 7)."""
    context = get_context(ctx)
    session = get_valid_session(ctx)
    start = date.today() - timedelta(days=days)

    orders = await _paginate(
        lambda offset: context.account.a_get_order_history(
            session, start_date=start, underlying_symbol=underlying_symbol,
            per_page=50, page_offset=offset
        ),
        page_size=50  # Order history API max is 50 per page
    )
    return to_table(orders)


@mcp_app.tool()
async def get_market_metrics(ctx: Context, symbols: list[str]) -> str:
    """
    Get market metrics including volatility (IV/HV), risk (beta, correlation),
    valuation (P/E, market cap), liquidity, dividends, earnings, and options data.

    Note extreme IV rank/percentile (0-1): low = cheap options (buy opportunity), high = expensive options (close positions).
    """
    session = get_valid_session(ctx)
    metrics = await a_get_market_metrics(session, symbols)
    return to_table(metrics)


def _get_next_open_time(session, current_time: datetime) -> datetime | None:
    """Determine next market open time based on current status."""
    if session.status == MarketStatus.PRE_MARKET:
        return session.open_at
    if session.status == MarketStatus.CLOSED:
        if session.open_at and current_time < session.open_at:
            return session.open_at
        if session.close_at and current_time > session.close_at and session.next_session:
            return session.next_session.open_at
    if session.status == MarketStatus.EXTENDED and session.next_session:
        return session.next_session.open_at
    return None


@mcp_app.tool()
async def market_status(ctx: Context, exchanges: list[Literal['Equity', 'CME', 'CFE', 'Smalls']] | None = None):
    """
    Get market status for each exchange including current open/closed state,
    next opening times, and holiday information.
    """
    if exchanges is None:
        exchanges = ['Equity']
    session = get_valid_session(ctx)
    market_sessions = await a_get_market_sessions(session, [ExchangeType(exchange) for exchange in exchanges])

    if not market_sessions:
        logger.error(f"No market sessions found for exchanges: {exchanges}")
        raise ValueError("No market sessions found")

    current_time = datetime.now(UTC)
    calendar = await a_get_market_holidays(session)
    is_holiday = current_time.date() in calendar.holidays
    is_half_day = current_time.date() in calendar.half_days

    results: list[dict[str, Any]] = []
    for ms in market_sessions:
        result: dict[str, Any] = {"exchange": ms.instrument_collection, "status": ms.status.value}

        if ms.status == MarketStatus.OPEN:
            if ms.close_at:
                result["close_at"] = ms.close_at.isoformat()
        else:
            open_at = _get_next_open_time(ms, current_time)
            if open_at:
                result["next_open"] = open_at.isoformat()
                result["time_until_open"] = humanize.naturaldelta(open_at - current_time)
            if is_holiday:
                result["is_holiday"] = True
            if is_half_day:
                result["is_half_day"] = True

        results.append(result)
    return results


@mcp_app.tool()
async def search_symbols(ctx: Context, symbol: str) -> str:
    """Search for symbols similar to the given search phrase."""
    session = get_valid_session(ctx)
    async with rate_limiter:
        results = await a_symbol_search(session, symbol)
    return to_table(results)


# =============================================================================
# TRADING TOOLS
# =============================================================================

def build_order_legs(instrument_details: list[InstrumentDetail], legs: list[OrderLeg]) -> list:
    """Build order legs from instrument details and leg specifications."""
    if len(instrument_details) != len(legs):
        raise ValueError(f"Mismatched legs: {len(instrument_details)} instruments vs {len(legs)} leg specs")

    built_legs = []
    for detail, leg_spec in zip(instrument_details, legs, strict=True):
        instrument = detail.instrument
        order_action = (
            OrderAction(leg_spec.action) if isinstance(instrument, Option)
            else OrderAction.BUY if leg_spec.action == 'Buy' else OrderAction.SELL
        )
        built_legs.append(instrument.build_leg(Decimal(str(leg_spec.quantity)), order_action))
    return built_legs


async def _fetch_quotes_raw(session: Session, instrument_details: list[InstrumentDetail], timeout: float = 10.0) -> list[Any]:
    """Fetch raw Quote objects for price calculations (internal use)."""
    return await _stream_events(session, Quote, [d.streamer_symbol for d in instrument_details], timeout)


async def calculate_net_price(ctx: Context, instrument_details: list[InstrumentDetail], legs: list[OrderLeg]) -> float:
    """Calculate net price from current market quotes."""
    session = get_valid_session(ctx)
    quotes = await _fetch_quotes_raw(session, instrument_details)

    net_price = 0.0
    for quote, detail, leg in zip(quotes, instrument_details, legs, strict=True):
        if quote.bid_price is not None and quote.ask_price is not None:
            mid_price = float(quote.bid_price + quote.ask_price) / 2
            leg_price = -mid_price if leg.action.startswith('Buy') else mid_price
            net_price += leg_price * leg.quantity
        else:
            inst = detail.instrument
            symbol_info = (
                f"{inst.underlying_symbol} {inst.option_type.value}{inst.strike_price} {inst.expiration_date}"
                if isinstance(inst, Option) else inst.symbol
            )
            logger.warning(f"Could not get bid/ask prices for {symbol_info}")
            raise ValueError(f"Could not get bid/ask for {symbol_info}")

    return round(net_price * 100) / 100


@mcp_app.tool()
async def get_live_orders(ctx: Context) -> str:
    context = get_context(ctx)
    session = get_valid_session(ctx)
    orders = await context.account.a_get_live_orders(session)
    return to_table(orders)


@mcp_app.tool()
async def place_order(
    ctx: Context,
    legs: list[OrderLeg],
    price: float | None = None,
    time_in_force: Literal['Day', 'GTC', 'IOC'] = 'Day',
    dry_run: bool = False
) -> dict[str, Any]:
    """
    Place multi-leg options/equity orders.

    Args:
        legs: List of leg specifications. Each leg contains:
            - symbol: str - Stock symbol (e.g., 'TQQQ', 'AAPL')
            - action: For stocks: 'Buy' or 'Sell'
                     For options: 'Buy to Open', 'Buy to Close', 'Sell to Open', 'Sell to Close'
            - quantity: int - Number of contracts/shares
            - option_type: 'C' or 'P' (optional, omit for stocks)
            - strike_price: float (required for options)
            - expiration_date: str - YYYY-MM-DD format (required for options)
        price: If None, calculates net mid-price from quotes.
               For debit orders (net buying), use negative values (e.g., -8.50).
               For credit orders (net selling), use positive values (e.g., 2.25).
               NOTE: If Tastytrade returns a price granularity error, retry with price
               rounded to nearest 5 cents.
        time_in_force: 'Day', 'GTC', or 'IOC'
        dry_run: If True, validates order without placing it

    Examples:
        Auto-priced stock: place_order([{"symbol": "AAPL", "action": "Buy", "quantity": 100}])
        Manual-priced option: place_order([{"symbol": "TQQQ", "option_type": "C", "action": "Buy to Open", "quantity": 17, "strike_price": 100.0, "expiration_date": "2026-01-16"}], -8.50)
        Auto-priced spread: place_order([
            {"symbol": "AAPL", "option_type": "C", "action": "Buy to Open", "quantity": 1, "strike_price": 150.0, "expiration_date": "2024-12-20"},
            {"symbol": "AAPL", "option_type": "C", "action": "Sell to Open", "quantity": 1, "strike_price": 155.0, "expiration_date": "2024-12-20"}
        ])
    """
    async with rate_limiter:
        if not legs:
            logger.error("place_order called with empty legs list")
            raise ValueError("At least one leg is required")

        context = get_context(ctx)
        session = get_valid_session(ctx)
        # Convert OrderLeg to InstrumentSpec for instrument lookup
        instrument_specs = [
            InstrumentSpec(
                symbol=leg.symbol,
                option_type=leg.option_type,
                strike_price=leg.strike_price,
                expiration_date=leg.expiration_date
            )
            for leg in legs
        ]
        instrument_details = await get_instrument_details(session, instrument_specs)

        # Calculate price if not provided
        if price is None:
            try:
                price = await calculate_net_price(ctx, instrument_details, legs)
                await ctx.info(f"💰 Auto-calculated net mid-price: ${price:.2f}")
                logger.info(f"Auto-calculated price ${price:.2f} for {len(legs)}-leg order")
            except Exception as e:
                logger.warning(f"Failed to auto-calculate price for order legs {[leg.symbol for leg in legs]}: {e!s}")
                raise ValueError(f"Could not fetch quotes for price calculation: {e!s}. Please provide a price.") from e

        return (await context.account.a_place_order(
            session,
            NewOrder(
                time_in_force=OrderTimeInForce(time_in_force),
                order_type=OrderType.LIMIT,
                legs=build_order_legs(instrument_details, legs),
                price=Decimal(str(price))
            ),
            dry_run=dry_run
        )).model_dump()


@mcp_app.tool()
async def replace_order(
    ctx: Context,
    order_id: str,
    price: float
) -> dict[str, Any]:
    """
    Replace (modify) an existing order with a new price.
    For complex changes like different legs/quantities, cancel and place a new order instead.

    Args:
        order_id: ID of the order to replace
        price: New limit price. Use negative values for debit orders (net buying),
               positive values for credit orders (net selling).

    Examples:
        Increase price to get filled: replace_order("12345", -10.05)
        Reduce price: replace_order("12345", -9.50)
    """
    async with rate_limiter:
        context = get_context(ctx)
        session = get_valid_session(ctx)

        # Get the existing order
        live_orders = await context.account.a_get_live_orders(session)
        existing_order = next((order for order in live_orders if str(order.id) == order_id), None)

        if not existing_order:
            live_order_ids = [str(order.id) for order in live_orders]
            logger.warning(f"Order {order_id} not found in live orders. Available orders: {live_order_ids}")
            raise ValueError(f"Order {order_id} not found in live orders")

        # Replace order with modified price
        return (await context.account.a_replace_order(
            session,
            int(order_id),
            NewOrder(
                time_in_force=existing_order.time_in_force,
                order_type=existing_order.order_type,
                legs=existing_order.legs,
                price=Decimal(str(price))
            )
        )).model_dump()


@mcp_app.tool()
async def delete_order(ctx: Context, order_id: str) -> dict[str, Any]:
    """Cancel an existing order."""
    context = get_context(ctx)
    session = get_valid_session(ctx)
    await context.account.a_delete_order(session, int(order_id))
    return {"success": True, "order_id": order_id}


# =============================================================================
# WATCHLIST TOOLS
# =============================================================================

@mcp_app.tool()
async def get_watchlists(
    ctx: Context,
    watchlist_type: Literal['public', 'private'] = 'private',
    name: str | None = None
) -> list[dict[str, Any]]:
    """
    Get watchlists for market insights and tracking.

    No name = list all watchlist names. With name = get that specific watchlist (wrapped in list).
    For private watchlists, "main" is the default.
    """
    session = get_valid_session(ctx)
    watchlist_class = PublicWatchlist if watchlist_type == 'public' else PrivateWatchlist

    if name:
        return [(await watchlist_class.a_get(session, name)).model_dump()]
    return [w.model_dump() for w in await watchlist_class.a_get(session)]


@mcp_app.tool()
async def manage_private_watchlist(
    ctx: Context,
    action: Literal["add", "remove"],
    symbols: list[WatchlistSymbol],
    name: str = "main"
) -> None:
    """
    Add or remove multiple symbols from a private watchlist.

    Args:
        action: "add" or "remove"
        symbols: List of symbol specifications. Each contains:
            - symbol: str - Stock symbol (e.g., "AAPL", "TSLA")
            - instrument_type: str - One of: "Equity", "Equity Option", "Future", "Future Option", "Cryptocurrency", "Warrant"
        name: Watchlist name (defaults to "main")

    Examples:
        Add stocks: manage_private_watchlist("add", [
            {"symbol": "AAPL", "instrument_type": "Equity"},
            {"symbol": "TSLA", "instrument_type": "Equity"}
        ], "tech-stocks")

        Remove options: manage_private_watchlist("remove", [
            {"symbol": "SPY", "instrument_type": "Equity Option"}
        ])
    """
    session = get_valid_session(ctx)

    if not symbols:
        logger.error("manage_private_watchlist called with empty symbols list")
        raise ValueError("At least one symbol is required")

    if action == "add":
        try:
            watchlist = await PrivateWatchlist.a_get(session, name)
            for symbol_spec in symbols:
                symbol = symbol_spec.symbol
                instrument_type = InstrumentType(symbol_spec.instrument_type)
                watchlist.add_symbol(symbol, instrument_type)
            await watchlist.a_update(session)
            logger.info(f"Added {len(symbols)} symbols to existing watchlist '{name}'")

            symbol_list = [f"{s.symbol} ({s.instrument_type})" for s in symbols]
            await ctx.info(f"✅ Added {len(symbols)} symbols to watchlist '{name}': {', '.join(symbol_list)}")
        except Exception as e:
            logger.info(f"Watchlist '{name}' not found, creating new one: {e}")
            watchlist_entries = [{"symbol": s.symbol, "instrument_type": s.instrument_type} for s in symbols]
            watchlist = PrivateWatchlist(
                name=name,
                group_name="main",
                watchlist_entries=watchlist_entries
            )
            await watchlist.a_upload(session)
            logger.info(f"Created new watchlist '{name}' with {len(symbols)} symbols")
            symbol_list = [f"{s.symbol} ({s.instrument_type})" for s in symbols]
            await ctx.info(f"✅ Created watchlist '{name}' and added {len(symbols)} symbols: {', '.join(symbol_list)}")
    else:
        try:
            watchlist = await PrivateWatchlist.a_get(session, name)
            for symbol_spec in symbols:
                symbol = symbol_spec.symbol
                instrument_type = InstrumentType(symbol_spec.instrument_type)
                watchlist.remove_symbol(symbol, instrument_type)
            await watchlist.a_update(session)
            logger.info(f"Removed {len(symbols)} symbols from watchlist '{name}'")

            symbol_list = [f"{s.symbol} ({s.instrument_type})" for s in symbols]
            await ctx.info(f"✅ Removed {len(symbols)} symbols from watchlist '{name}': {', '.join(symbol_list)}")
        except Exception as e:
            logger.error(f"Failed to remove symbols from watchlist '{name}': {e}")
            raise


@mcp_app.tool()
async def delete_private_watchlist(ctx: Context, name: str) -> None:
    session = get_valid_session(ctx)
    await PrivateWatchlist.a_remove(session, name)
    await ctx.info(f"✅ Deleted private watchlist '{name}'")


# =============================================================================
# UTILITY TOOLS
# =============================================================================

@mcp_app.tool()
async def get_current_time_nyc() -> str:
    return now_in_new_york().isoformat()


# =============================================================================
# ANALYSIS TOOLS
# =============================================================================

@mcp_app.prompt(title="IV Rank Analysis")
def analyze_iv_opportunities() -> list[base.Message]:
    return [
        base.UserMessage("""Please analyze IV rank, percentile, and liquidity for:
1. All active positions in my account
2. All symbols in my watchlists

Focus on identifying extremes:
- Low IV rank (<.2) may present entry opportunities (cheap options)
- High IV rank (>.8) may present exit opportunities (expensive options)
- Also consider liquidity levels to ensure tradeable positions

Use the get_positions, get_watchlists, and get_market_metrics tools to gather this data."""),
        base.AssistantMessage("""I'll analyze IV opportunities for your positions and watchlist. Let me start by gathering your current positions and watchlist data, then get market metrics for each symbol to assess IV rank extremes and liquidity.""")
    ]
