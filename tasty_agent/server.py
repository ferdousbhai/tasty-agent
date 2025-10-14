import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta, datetime, date, timezone
from decimal import Decimal
import logging
import os
from typing import Literal, AsyncIterator, Any, Sequence, TypedDict
from pydantic import BaseModel, Field

from aiocache import cached, Cache
from aiocache.serializers import PickleSerializer
from aiolimiter import AsyncLimiter
import humanize
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.fastmcp.prompts import base
from mcp.server.session import ServerSession
from mcp.types import SamplingMessage, TextContent
from tabulate import tabulate
from tastytrade import OAuthSession, Account
from tastytrade.dxfeed import Quote, Greeks
from tastytrade.instruments import Equity, Option, a_get_option_chain
from tastytrade.market_sessions import a_get_market_sessions, a_get_market_holidays, ExchangeType, MarketStatus
from tastytrade.metrics import a_get_market_metrics
from tastytrade.order import NewOrder, OrderAction, OrderTimeInForce, OrderType
from tastytrade.search import a_symbol_search
from tastytrade.streamer import DXLinkStreamer
from tastytrade.utils import now_in_new_york
from tastytrade.watchlists import PublicWatchlist, PrivateWatchlist

logger = logging.getLogger(__name__)

rate_limiter = AsyncLimiter(2, 1) # 2 requests per second

def to_table(data: Sequence[BaseModel]) -> str:
    """Format list of Pydantic models as a plain table."""
    if not data:
        return "No data"
    return tabulate([item.model_dump() for item in data], headers='keys', tablefmt='plain')

@dataclass
class ServerContext:
    session: OAuthSession
    account: Account


def get_context(ctx: Context) -> ServerContext:
    """Extract context from request."""
    return ctx.request_context.lifespan_context

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
        session = OAuthSession(client_secret, refresh_token)
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
    return {k: v for k, v in (await context.account.a_get_balances(context.session)).model_dump().items() if v is not None and v != 0}


@mcp_app.tool()
async def get_positions(ctx: Context) -> str:
    context = get_context(ctx)
    positions = await context.account.a_get_positions(context.session, include_marks=True)
    return to_table(positions)


@mcp_app.tool()
async def get_net_liquidating_value_history(
    ctx: Context,
    time_back: Literal['1d', '1m', '3m', '6m', '1y', 'all'] = '1y'
) -> str:
    """Portfolio value over time. ⚠️ Use with get_transaction_history(transaction_type="Money Movement") to separate trading performance from deposits/withdrawals."""
    context = get_context(ctx)
    history = await context.account.a_get_net_liquidating_value_history(context.session, time_back=time_back)
    return to_table(history)



# =============================================================================
# MARKET DATA TOOLS
# =============================================================================

class InstrumentDetail(TypedDict):
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
    except ValueError:
        raise ValueError(f"Invalid date format '{date_string}'. Expected YYYY-MM-DD format.")


def validate_strike_price(strike_price: Any) -> float:
    """Validate and convert strike price to float."""
    try:
        strike = float(strike_price)
        if strike <= 0:
            raise ValueError("Strike price must be positive")
        return strike
    except (ValueError, TypeError):
        raise ValueError(f"Invalid strike price '{strike_price}'. Expected positive number.")


@cached(ttl=86400, cache=Cache.MEMORY, serializer=PickleSerializer())  # 24 hours
async def get_cached_option_chain(session: OAuthSession, symbol: str):
    """Cache option chains for 24 hours as they rarely change during that timeframe."""
    return await a_get_option_chain(session, symbol)


async def get_instrument_details(session: OAuthSession, instrument_specs: list[InstrumentSpec]) -> list[InstrumentDetail]:
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
                    return InstrumentDetail(
                        streamer_symbol=option.streamer_symbol,
                        instrument=option
                    )

            available_strikes = [opt.strike_price for opt in chain[target_date] if opt.option_type.value == option_type]
            raise ValueError(f"Option not found: {symbol} {expiration_date} {option_type} {strike_price}. Available strikes: {sorted(set(available_strikes))}")
        else:
            # Get equity instrument
            instrument = await Equity.a_get(session, symbol)
            return InstrumentDetail(
                streamer_symbol=symbol,
                instrument=instrument
            )

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

    context = get_context(ctx)
    instrument_details = await get_instrument_details(context.session, instruments)

    try:
        async with DXLinkStreamer(context.session) as streamer:
            await streamer.subscribe(Quote, [d["streamer_symbol"] for d in instrument_details])

            # Collect quotes by symbol (handle out-of-order arrivals)
            quotes_by_symbol = {}
            expected_symbols = {d["streamer_symbol"] for d in instrument_details}
            while len(quotes_by_symbol) < len(instrument_details):
                quote = await asyncio.wait_for(streamer.get_event(Quote), timeout=timeout)
                if quote.event_symbol in expected_symbols:
                    quotes_by_symbol[quote.event_symbol] = quote

            # Return quotes in same order as input instruments
            return to_table([quotes_by_symbol[d["streamer_symbol"]]
                            for d in instrument_details])

    except asyncio.TimeoutError:
        logger.warning(f"Timeout getting quotes for {len(instruments)} instruments after {timeout}s")
        raise ValueError(f"Timeout getting quotes after {timeout}s")
    except Exception as e:
        logger.error(f"Error getting quotes for instruments {[i.symbol for i in instruments]}: {str(e)}")
        raise ValueError(f"Error getting quotes: {str(e)}")


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

    context = get_context(ctx)
    option_details = await get_instrument_details(context.session, options)

    try:
        async with DXLinkStreamer(context.session) as streamer:
            await streamer.subscribe(Greeks, [d["streamer_symbol"] for d in option_details])

            # Collect Greeks by symbol
            greeks_by_symbol = {}
            expected_symbols = {d["streamer_symbol"] for d in option_details}
            while len(greeks_by_symbol) < len(option_details):
                greeks = await asyncio.wait_for(streamer.get_event(Greeks), timeout=timeout)
                if greeks.event_symbol in expected_symbols:
                    greeks_by_symbol[greeks.event_symbol] = greeks

            # Return Greeks in same order as input options
            return to_table([greeks_by_symbol[d["streamer_symbol"]]
                            for d in option_details])

    except asyncio.TimeoutError:
        logger.warning(f"Timeout getting Greeks for {len(options)} options after {timeout}s")
        raise ValueError(f"Timeout getting Greeks after {timeout}s")
    except Exception as e:
        logger.error(f"Error getting Greeks for options {[opt.symbol for opt in options]}: {str(e)}")
        raise ValueError(f"Error getting Greeks: {str(e)}")


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
    all_trades = []
    page_offset = 0
    PER_PAGE = 250

    while True:
        async with rate_limiter:
            trades = await context.account.a_get_history(
                context.session,
                start_date=date.today() - timedelta(days=days),
                underlying_symbol=underlying_symbol,
                type=transaction_type,
                per_page=PER_PAGE,
                page_offset=page_offset
            )

        if not trades or len(trades) < PER_PAGE:
            all_trades.extend(trades or [])
            break

        all_trades.extend(trades)
        page_offset += PER_PAGE

    return to_table(all_trades)


@mcp_app.tool()
async def get_order_history(
    ctx: Context,
    days: int = 7,
    underlying_symbol: str | None = None
) -> str:
    """Get all order history for the last N days (default: 7)."""
    context = get_context(ctx)
    all_orders = []
    page_offset = 0
    ORDER_PAGE_SIZE = 50  # Order history API max is 50 per page

    while True:
        async with rate_limiter:
            orders = await context.account.a_get_order_history(
                context.session,
                start_date=date.today() - timedelta(days=days),
                underlying_symbol=underlying_symbol,
                per_page=ORDER_PAGE_SIZE,
                page_offset=page_offset
            )

        if not orders or len(orders) < ORDER_PAGE_SIZE:
            all_orders.extend(orders or [])
            break

        all_orders.extend(orders)
        page_offset += ORDER_PAGE_SIZE

    return to_table(all_orders)


@mcp_app.tool()
async def get_market_metrics(ctx: Context, symbols: list[str]) -> str:
    """
    Get market metrics including volatility (IV/HV), risk (beta, correlation),
    valuation (P/E, market cap), liquidity, dividends, earnings, and options data.

    Note extreme IV rank/percentile (0-1): low = cheap options (buy opportunity), high = expensive options (close positions).
    """
    metrics = await a_get_market_metrics(get_context(ctx).session, symbols)
    return to_table(metrics)


@mcp_app.tool()
async def market_status(ctx: Context, exchanges: list[Literal['Equity', 'CME', 'CFE', 'Smalls']] = ['Equity']):
    """
    Get market status for each exchange including current open/closed state,
    next opening times, and holiday information.
    """
    context = get_context(ctx)
    market_sessions = await a_get_market_sessions(context.session, [ExchangeType(exchange) for exchange in exchanges])

    if not market_sessions:
        logger.error(f"No market sessions found for exchanges: {exchanges}")
        raise ValueError("No market sessions found")

    current_time = datetime.now(timezone.utc)
    calendar = await a_get_market_holidays(context.session)
    is_holiday = current_time.date() in calendar.holidays
    is_half_day = current_time.date() in calendar.half_days

    results = []
    for market_session in market_sessions:
        if market_session.status == MarketStatus.OPEN:
            result = {
                "exchange": market_session.instrument_collection,
                "status": market_session.status.value,
                "close_at": market_session.close_at.isoformat() if market_session.close_at else None,
            }
        else:
            open_at = (
                market_session.open_at if market_session.status == MarketStatus.PRE_MARKET and market_session.open_at else
                market_session.open_at if market_session.status == MarketStatus.CLOSED and market_session.open_at and current_time < market_session.open_at else
                market_session.next_session.open_at if market_session.status == MarketStatus.CLOSED and market_session.close_at and current_time > market_session.close_at and market_session.next_session and market_session.next_session.open_at else
                market_session.next_session.open_at if market_session.status == MarketStatus.EXTENDED and market_session.next_session and market_session.next_session.open_at else
                None
            )

            result = {
                "exchange": market_session.instrument_collection,
                "status": market_session.status.value,
                **({"next_open": open_at.isoformat(), "time_until_open": humanize.naturaldelta(open_at - current_time)} if open_at else {}),
                **({"is_holiday": True} if is_holiday else {}),
                **({"is_half_day": True} if is_half_day else {})
            }
        results.append(result)
    return results


@mcp_app.tool()
async def search_symbols(ctx: Context, symbol: str) -> str:
    """Search for symbols similar to the given search phrase."""
    async with rate_limiter:
        results = await a_symbol_search(get_context(ctx).session, symbol)
    return to_table(results)


# =============================================================================
# TRADING TOOLS
# =============================================================================

def build_order_legs(instrument_details: list[InstrumentDetail], legs: list[OrderLeg]) -> list:
    """Build order legs from instrument details and leg specifications."""
    built_legs = []
    for detail, leg_spec in zip(instrument_details, legs):
        action = leg_spec.action
        quantity = leg_spec.quantity
        instrument = detail['instrument']

        # Determine order action based on instrument type
        if isinstance(instrument, Option):
            order_action = OrderAction(action)
        else:
            order_action = OrderAction.BUY if action == 'Buy' else OrderAction.SELL

        built_legs.append(instrument.build_leg(Decimal(str(quantity)), order_action))
    return built_legs


async def calculate_net_price(ctx: Context, instrument_details: list[InstrumentDetail], legs: list[OrderLeg]) -> float:
    """Calculate net price from current market quotes."""
    # Convert instrument_details format for get_quotes
    instruments = []
    for detail in instrument_details:
        instrument_obj = detail['instrument']
        if isinstance(instrument_obj, Option):
            instrument = InstrumentSpec(
                symbol=instrument_obj.underlying_symbol,
                option_type=instrument_obj.option_type.value,
                strike_price=float(instrument_obj.strike_price),
                expiration_date=instrument_obj.expiration_date.strftime("%Y-%m-%d")
            )
        else:
            instrument = InstrumentSpec(symbol=instrument_obj.symbol)
        instruments.append(instrument)

    # Get quotes using existing tool
    quotes_data = await get_quotes(ctx, instruments)

    # Calculate net price
    net_price = 0.0
    for quote_data, leg_spec in zip(quotes_data, legs):
        if quote_data.get("bid_price") and quote_data.get("ask_price"):
            mid_price = (quote_data["bid_price"] + quote_data["ask_price"]) / 2
            leg_price = -mid_price if leg_spec.action.startswith('Buy') else mid_price
            net_price += leg_price * leg_spec.quantity
        else:
            instrument_obj = instrument_details[quotes_data.index(quote_data)]['instrument']
            if isinstance(instrument_obj, Option):
                symbol_info = f"{instrument_obj.underlying_symbol} {instrument_obj.option_type.value}{instrument_obj.strike_price} {instrument_obj.expiration_date}"
            else:
                symbol_info = instrument_obj.symbol
            logger.warning(f"Could not get bid/ask prices for {symbol_info} - quote data: {quote_data}")
            raise ValueError(f"Could not get bid/ask for {symbol_info}")

    return round(net_price * 100) / 100


@mcp_app.tool()
async def get_live_orders(ctx: Context) -> str:
    context = get_context(ctx)
    orders = await context.account.a_get_live_orders(context.session)
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
        instrument_details = await get_instrument_details(context.session, legs)

        # Calculate price if not provided
        if price is None:
            try:
                price = await calculate_net_price(ctx, instrument_details, legs)
                await ctx.info(f"💰 Auto-calculated net mid-price: ${price:.2f}")
                logger.info(f"Auto-calculated price ${price:.2f} for {len(legs)}-leg order")
            except Exception as e:
                logger.warning(f"Failed to auto-calculate price for order legs {[leg.symbol for leg in legs]}: {str(e)}")
                raise ValueError(f"Could not fetch quotes for price calculation: {str(e)}. Please provide a price.")

        return (await context.account.a_place_order(
            context.session,
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

        # Get the existing order
        live_orders = await context.account.a_get_live_orders(context.session)
        existing_order = next((order for order in live_orders if str(order.id) == order_id), None)

        if not existing_order:
            live_order_ids = [str(order.id) for order in live_orders]
            logger.warning(f"Order {order_id} not found in live orders. Available orders: {live_order_ids}")
            raise ValueError(f"Order {order_id} not found in live orders")

        # Replace order with modified price
        return (await context.account.a_replace_order(
            context.session,
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
    await context.account.a_delete_order(context.session, int(order_id))
    return {"success": True, "order_id": order_id}


# =============================================================================
# WATCHLIST TOOLS
# =============================================================================

@mcp_app.tool()
async def get_watchlists(
    ctx: Context,
    watchlist_type: Literal['public', 'private'] = 'private',
    name: str | None = None
) -> list[dict[str, Any]] | dict[str, Any]:
    """
    Get watchlists for market insights and tracking.

    No name = list watchlist names. With name = get symbols in that watchlist. For private, default to "main".
    """
    context = get_context(ctx)
    watchlist_class = PublicWatchlist if watchlist_type == 'public' else PrivateWatchlist

    return (await watchlist_class.a_get(context.session, name)).model_dump() if name else [w.model_dump() for w in await watchlist_class.a_get(context.session)]


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
    context = get_context(ctx)

    if not symbols:
        logger.error("manage_private_watchlist called with empty symbols list")
        raise ValueError("At least one symbol is required")

    if action == "add":
        try:
            watchlist = await PrivateWatchlist.a_get(context.session, name)
            for symbol_spec in symbols:
                symbol = symbol_spec.symbol
                instrument_type = symbol_spec.instrument_type
                watchlist.add_symbol(symbol, instrument_type)
            await watchlist.a_update(context.session)
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
            await watchlist.a_upload(context.session)
            logger.info(f"Created new watchlist '{name}' with {len(symbols)} symbols")
            symbol_list = [f"{s.symbol} ({s.instrument_type})" for s in symbols]
            await ctx.info(f"✅ Created watchlist '{name}' and added {len(symbols)} symbols: {', '.join(symbol_list)}")
    else:
        try:
            watchlist = await PrivateWatchlist.a_get(context.session, name)
            for symbol_spec in symbols:
                symbol = symbol_spec.symbol
                instrument_type = symbol_spec.instrument_type
                watchlist.remove_symbol(symbol, instrument_type)
            await watchlist.a_update(context.session)
            logger.info(f"Removed {len(symbols)} symbols from watchlist '{name}'")

            symbol_list = [f"{s.symbol} ({s.instrument_type})" for s in symbols]
            await ctx.info(f"✅ Removed {len(symbols)} symbols from watchlist '{name}': {', '.join(symbol_list)}")
        except Exception as e:
            logger.error(f"Failed to remove symbols from watchlist '{name}': {e}")
            raise


@mcp_app.tool()
async def delete_private_watchlist(ctx: Context, name: str) -> None:
    context = get_context(ctx)
    await PrivateWatchlist.a_remove(context.session, name)
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


@mcp_app.tool()
async def generate_trade_ideas(
    ctx: Context[ServerSession, None],
    focus_symbols: list[str] | None = None,
    risk_tolerance: Literal["conservative", "moderate", "aggressive"] = "moderate",
    max_ideas: int = 5
) -> str:
    """
    Generate specific, actionable trade ideas using AI analysis of current positions,
    watchlists, market metrics, and volatility environment.

    Args:
        focus_symbols: Optional list of symbols to focus analysis on. If None, analyzes all positions and watchlist symbols.
        risk_tolerance: Risk preference for trade suggestions
        max_ideas: Maximum number of trade ideas to generate
    """
    context = get_context(ctx)

    # Track data collection failures
    data_failures = []

    # Gather comprehensive market data using internal tastytrade methods
    try:
        # Get current positions directly from tastytrade
        try:
            positions = await context.account.a_get_positions(context.session, include_marks=True)
            position_symbols = list(set([pos.symbol for pos in positions]))
        except Exception as e:
            logger.error(f"Failed to get positions: {e}")
            data_failures.append(f"Could not retrieve current positions: {str(e)}")
            positions = []
            position_symbols = []

        # Get watchlist symbols directly from tastytrade
        try:
            main_watchlist = await PrivateWatchlist.a_get(context.session, "main")
            watchlist_symbols = [entry['symbol'] for entry in main_watchlist.watchlist_entries or []]
        except Exception as e:
            logger.error(f"Failed to get watchlist: {e}")
            data_failures.append(f"Could not retrieve watchlist data: {str(e)}")
            watchlist_symbols = []

        # Determine symbols to analyze
        if focus_symbols:
            analysis_symbols = focus_symbols
        else:
            analysis_symbols = list(set(position_symbols + watchlist_symbols))

        if not analysis_symbols:
            if data_failures:
                return "Cannot generate trade ideas due to data collection failures:\n" + "\n".join(data_failures)
            return "No symbols found to analyze. Add symbols to watchlist or specify focus_symbols."

        # Get market metrics directly from tastytrade (limit to avoid rate limits)
        try:
            market_metrics = await a_get_market_metrics(context.session, analysis_symbols[:10])
        except Exception as e:
            logger.error(f"Failed to get market metrics: {e}")
            data_failures.append(f"Could not retrieve market metrics: {str(e)}")
            market_metrics = []

        # Get current time
        current_time = now_in_new_york().isoformat()

        # Prepare data summary for LLM
        positions_summary = []
        for pos in positions:
            positions_summary.append({
                "symbol": pos.symbol,
                "quantity": float(pos.quantity),
                "instrument_type": pos.instrument_type.value,
                "mark_price": float(pos.mark_price) if pos.mark_price else None,
                "average_open_price": float(pos.average_open_price),
                "underlying_symbol": pos.underlying_symbol
            })

        metrics_summary = []
        for metric in market_metrics:
            metrics_summary.append({
                "symbol": metric.symbol,
                "iv_rank": metric.implied_volatility_index_rank,
                "iv_percentile": metric.implied_volatility_percentile,
                "liquidity_value": metric.liquidity_value,
                "liquidity_rank": metric.liquidity_rank,
                "liquidity_rating": metric.liquidity_rating,
                "market_cap": float(metric.market_cap) if metric.market_cap else None,
                "beta": float(metric.beta) if metric.beta else None,
                "lendability": metric.lendability
            })

        # Include data availability status in prompt
        data_status = ""
        if data_failures:
            data_status = "\n\nDATA COLLECTION FAILURES:\n" + "\n".join(f"- {failure}" for failure in data_failures)

        # Create comprehensive prompt for trade idea generation
        analysis_prompt = f"""You are an expert options trader analyzing market opportunities. Current time: {current_time}

CURRENT POSITIONS:
{positions_summary}

MARKET METRICS:
{metrics_summary}

RISK TOLERANCE: {risk_tolerance}{data_status}

IMPORTANT: If any required data is missing or failed to collect (as noted above), you MUST clearly state what data is unavailable and explain that you cannot generate reliable trade ideas without this information. DO NOT fabricate or guess at missing data like IV ranks, positions, or market metrics. Instead, inform the user about the data collection failures and suggest they try again or check their connection/authentication.

Only generate trade ideas if you have sufficient real data. If generating ideas, provide {max_ideas} specific, actionable trade ideas based on the available data:

1. SYMBOL & STRATEGY: Clear trade description (e.g., "AAPL Iron Condor", "TSLA Short Strangle")
2. RATIONALE: Why this trade makes sense given current IV rank, positions, and market conditions
3. SPECIFIC DETAILS: Strike prices, expiration, entry criteria
4. RISK/REWARD: Expected profit/loss profile and maximum risk
5. MANAGEMENT: When to adjust or exit

Focus on:
- IV rank extremes (high IV rank = selling opportunities, low IV rank = buying opportunities)
- Portfolio balance and correlation
- Liquidity considerations
- Risk-appropriate strategies for {risk_tolerance} tolerance

Be specific and actionable - provide exact strikes and expirations where possible."""

        # Use LLM sampling to generate trade ideas
        result = await ctx.session.create_message(
            messages=[
                SamplingMessage(
                    role="user",
                    content=TextContent(type="text", text=analysis_prompt),
                )
            ],
            max_tokens=1500,
        )

        if result.content.type == "text":
            return result.content.text
        return str(result.content)

    except Exception as e:
        logger.error(f"Error generating trade ideas: {e}")
        return f"Error generating trade ideas: {str(e)}"