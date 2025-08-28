import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta, datetime, date, timezone
from decimal import Decimal
import keyring
import logging
import os
from typing import Literal, AsyncIterator, Any, TypedDict

import humanize
from mcp.server.fastmcp import FastMCP, Context
from tastytrade import Session, Account
from tastytrade.dxfeed import Quote
from tastytrade.instruments import NestedOptionChain, Equity, Option, Future, FutureOption, Cryptocurrency, Warrant, InstrumentType
from tastytrade.market_sessions import a_get_market_sessions, a_get_market_holidays, ExchangeType, MarketStatus
from tastytrade.metrics import a_get_market_metrics
from tastytrade.order import NewOrder, OrderAction, OrderTimeInForce, OrderType, Leg
from tastytrade.search import a_symbol_search
from tastytrade.streamer import DXLinkStreamer
from tastytrade.watchlists import PublicWatchlist, PrivateWatchlist, PairsWatchlist

logger = logging.getLogger(__name__)


@dataclass
class ServerContext:
    session: Session | None
    account: Account | None

def get_context(ctx: Context) -> ServerContext:
    """Helper to extract context from MCP request."""
    return ctx.request_context.lifespan_context

@asynccontextmanager
async def lifespan(_) -> AsyncIterator[ServerContext]:
    """Manages Tastytrade session lifecycle."""

    def get_credential(key: str, env_var: str) -> str | None:
        try:
            return keyring.get_password("tastytrade", key) or os.getenv(env_var)
        except Exception:
            return os.getenv(env_var)

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
        if not (account := next((acc for acc in accounts if acc.account_number == account_id), None)):
            raise ValueError(f"Specified Tastytrade account ID '{account_id}' not found.")
    else:
        account = accounts[0]
        if len(accounts) > 1:
            logger.info(f"Using account {account.account_number} (first of {len(accounts)})")

    yield ServerContext(
        session=session,
        account=account
    )

mcp = FastMCP("TastyTrade", lifespan=lifespan)

async def find_option_instrument(session: Session, symbol: str, expiration_date: str, option_type: Literal['C', 'P'], strike_price: float) -> Option:
    """Helper function to find an option instrument using the option chain."""
    from tastytrade.instruments import a_get_option_chain
    from datetime import datetime

    # Get option chain for the underlying
    chain = await a_get_option_chain(session, symbol)

    # Parse target expiration date
    target_date = datetime.strptime(expiration_date, "%Y-%m-%d").date()

    # Find the specific option
    if target_date not in chain:
        raise ValueError(f"No options found for expiration date {expiration_date}")

    for option in chain[target_date]:
        if (option.strike_price == strike_price and
            option.option_type.value == option_type.upper()):
            return option

    raise ValueError(f"Option not found: {symbol} {expiration_date} {option_type} {strike_price}")

@mcp.tool()
async def get_balances(ctx: Context) -> dict[str, Any]:
    context = get_context(ctx)
    balances = await context.account.a_get_balances(context.session)
    return {k: v for k, v in balances.model_dump().items() if v is not None and v != 0}

@mcp.tool()
async def get_positions(ctx: Context) -> list[dict[str, Any]]:
    context = get_context(ctx)
    positions = await context.account.a_get_positions(context.session, include_marks=True)
    return [pos.model_dump() for pos in positions]

@mcp.tool()
async def get_quote(
    ctx: Context,
    symbol: str,
    option_type: Literal['C', 'P'] | None = None,
    strike_price: float | None = None,
    expiration_date: str | None = None,
    timeout: float = 10.0
) -> dict[str, Any]:
    """
    Get live quote for a stock or option.

    Args:
        symbol: Stock symbol (e.g., 'AAPL', 'TQQQ')
        option_type: 'C' or 'P' (required for options)
        strike_price: Strike price (required for options)
        expiration_date: Expiration date in YYYY-MM-DD format (required for options)
        timeout: Timeout in seconds

    Examples:
        Stock: get_quote("AAPL")
        Option: get_quote("TQQQ", "C", 100.0, "2026-01-16")
    """
    context = get_context(ctx)

    # For options, find the option using helper function
    if option_type is not None:
        if strike_price is None or expiration_date is None:
            raise ValueError("strike_price and expiration_date are required for option quotes")

        option_instrument = await find_option_instrument(context.session, symbol, expiration_date, option_type, strike_price)
        streamer_symbol = option_instrument.streamer_symbol
    else:
        # For stocks, use the symbol directly
        streamer_symbol = symbol

    try:
        async with DXLinkStreamer(context.session) as streamer:
            await streamer.subscribe(Quote, [streamer_symbol])

            # Wait for the quote with timeout
            quote = await asyncio.wait_for(streamer.get_event(Quote), timeout=timeout)
            return quote.model_dump()

    except asyncio.TimeoutError:
        raise ValueError(f"Timeout getting quote for {streamer_symbol} after {timeout}s")
    except Exception as e:
        logger.error(f"Error getting quote for {streamer_symbol}: {e}")
        raise ValueError(f"Error getting quote: {str(e)}")

@mcp.tool()
async def get_net_liquidating_value_history(
    ctx: Context,
    time_back: Literal['1d', '1m', '3m', '6m', '1y', 'all'] = '1y'
) -> list[dict[str, Any]]:
    context = get_context(ctx)
    history = await context.account.a_get_net_liquidating_value_history(context.session, time_back=time_back)
    return [h.model_dump() for h in history]

@mcp.tool()
async def get_history(
    ctx: Context,
    start_date: str | None = None
) -> list[dict[str, Any]]:
    """start_date format: YYYY-MM-DD."""
    date_obj = date.today() - timedelta(days=90) if start_date is None else datetime.strptime(start_date, "%Y-%m-%d").date()
    context = get_context(ctx)
    transactions = await context.account.a_get_history(context.session, start_date=date_obj)
    return [txn.model_dump() for txn in transactions]

@mcp.tool()
async def get_market_metrics(ctx: Context, symbols: list[str]) -> list[dict[str, Any]]:
    """
    Get market metrics including volatility (IV/HV), risk (beta, correlation),
    valuation (P/E, market cap), liquidity, dividends, earnings, and options data.
    """
    context = get_context(ctx)
    metrics_data = await a_get_market_metrics(context.session, symbols)
    return [m.model_dump() for m in metrics_data]

@mcp.tool()
async def market_status(ctx: Context, exchanges: list[Literal['Equity', 'CME', 'CFE', 'Smalls']] = ['Equity']) -> list[dict[str, Any]]:
    """
    Get market status for each exchange including current open/closed state,
    next opening times, and holiday information.
    """
    context = get_context(ctx)
    exchange_types = [ExchangeType(exchange) for exchange in exchanges]
    market_sessions = await a_get_market_sessions(context.session, exchange_types)

    if not market_sessions:
        raise ValueError("No market sessions found")

    current_time = datetime.now(timezone.utc)
    current_date = current_time.date()
    calendar = await a_get_market_holidays(context.session)
    is_holiday = current_date in calendar.holidays
    is_half_day = current_date in calendar.half_days

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

@mcp.tool()
async def search_symbols(ctx: Context, symbol: str) -> list[dict[str, Any]]:
    """Search for symbols similar to the given search phrase."""
    context = get_context(ctx)
    results = await a_symbol_search(context.session, symbol)
    return [result.model_dump() for result in results]

@mcp.tool()
async def get_live_orders(ctx: Context) -> list[dict[str, Any]]:
    context = get_context(ctx)
    orders = await context.account.a_get_live_orders(context.session)
    return [order.model_dump() for order in orders]



@mcp.tool()
async def place_order(
    ctx: Context,
    symbol: str,
    order_type: Literal['C', 'P', 'Stock'],
    action: Literal['Buy', 'Sell'],
    quantity: int,
    price: float,
    strike_price: float | None = None,
    expiration_date: str | None = None,
    time_in_force: Literal['Day', 'GTC', 'IOC'] = 'Day',
    dry_run: bool = False
) -> dict[str, Any]:
    """
    Place an options or equity order with simplified parameters.

    Args:
        symbol: Stock symbol (e.g., 'TQQQ', 'AAPL')
        order_type: 'C', 'P', or 'Stock'
        action: 'Buy' or 'Sell'
        quantity: Number of contracts or shares
        price: Limit price
        strike_price: Strike price (required for options)
        expiration_date: Expiration date in YYYY-MM-DD format (required for options)
        time_in_force: 'Day', 'GTC', or 'IOC'
        dry_run: If True, validates order without placing it

    Examples:
        Options: place_order("TQQQ", "C", "Buy", 17, 8.55, 100.0, "2026-01-16")
        Stock: place_order("AAPL", "Stock", "Buy", 100, 150.00)
    """
    context = get_context(ctx)

    # Validation for options
    if order_type in ['C', 'P']:
        if strike_price is None:
            raise ValueError(f"strike_price is required for {order_type} orders")
        if expiration_date is None:
            raise ValueError(f"expiration_date is required for {order_type} orders")

        # Find the option using helper function
        instrument = await find_option_instrument(context.session, symbol, expiration_date, order_type, strike_price)

        # Map simplified action to tastytrade OrderAction
        if action == 'Buy':
            order_action = OrderAction.BUY_TO_OPEN
        else:  # action == 'Sell'
            order_action = OrderAction.SELL_TO_OPEN

        # Build option leg
        leg = instrument.build_leg(Decimal(str(quantity)), order_action)

    else:  # order_type == 'Stock'
        # Map simplified action to tastytrade OrderAction for stocks
        if action == 'Buy':
            order_action = OrderAction.BUY
        else:  # action == 'Sell'
            order_action = OrderAction.SELL

        # Build equity leg
        instrument = await Equity.a_get(context.session, symbol)
        leg = instrument.build_leg(Decimal(str(quantity)), order_action)

    # Create and place the order
    order = NewOrder(
        time_in_force=OrderTimeInForce(time_in_force),
        order_type=OrderType.LIMIT,
        legs=[leg],
        price=Decimal(str(price))
    )

    response = await context.account.a_place_order(context.session, order, dry_run=dry_run)
    return response.model_dump()

@mcp.tool()
async def delete_order(ctx: Context, order_id: str) -> dict[str, Any]:
    context = get_context(ctx)
    await context.account.a_delete_order(context.session, int(order_id))
    return {"success": True, "order_id": order_id}

@mcp.tool()
async def get_public_watchlists(
    ctx: Context,
    name: str | None = None
) -> list[dict[str, Any]] | dict[str, Any]:
    """
    Get public watchlists for market insights and tracking.
    If name is provided, returns specific watchlist; otherwise returns all public watchlists.
    """
    context = get_context(ctx)

    if name:
        watchlist = await PublicWatchlist.a_get(context.session, name)
        return watchlist.model_dump()
    else:
        watchlists = await PublicWatchlist.a_get(context.session)
        return [watchlist.model_dump() for watchlist in watchlists]

@mcp.tool()
async def get_private_watchlists(
    ctx: Context,
    name: str | None = None
) -> list[dict[str, Any]] | dict[str, Any]:
    """
    Get user's private watchlists for portfolio organization and tracking.
    If name is provided, returns specific watchlist; otherwise returns all private watchlists.
    """
    context = get_context(ctx)

    if name:
        watchlist = await PrivateWatchlist.a_get(context.session, name)
        return watchlist.model_dump()
    else:
        watchlists = await PrivateWatchlist.a_get(context.session)
        return [w.model_dump() for w in watchlists]

class WatchlistEntryDict(TypedDict):
    symbol: str
    instrument_type: str

@mcp.tool()
async def create_private_watchlist(
    ctx: Context,
    name: str,
    entries: list[WatchlistEntryDict] = [],
    group_name: str = "main"
) -> None:
    """
    Args:
        name: Name of the watchlist to create
        entries: List of dictionaries, each containing:
            - symbol: str - The ticker symbol (e.g., "AAPL", "SPY")
            - instrument_type: str - One of: "Equity", "Equity Option", "Future",
              "Future Option", "Cryptocurrency", "Warrant"
        group_name: Group name for organization (defaults to "main")
    """
    context = get_context(ctx)

    valid_instrument_types = ["Equity", "Equity Option", "Future", "Future Option", "Cryptocurrency", "Warrant"]

    watchlist_entries = []
    for entry in entries:
        if isinstance(entry, dict):
            symbol = entry.get("symbol")
            instrument_type = entry.get("instrument_type")

            if not symbol:
                raise ValueError(f"Missing required 'symbol' key in entry: {entry}")

            if not instrument_type:
                raise ValueError(f"Missing required 'instrument_type' key in entry: {entry}")

            if instrument_type not in valid_instrument_types:
                raise ValueError(f"Invalid instrument_type '{instrument_type}'. Valid types: {valid_instrument_types}")
        else:
            raise ValueError(f"Each symbol entry must be a dictionary with 'symbol' and 'instrument_type' keys. Got: {entry}")

        watchlist_entries.append({
            "symbol": symbol,
            "instrument_type": instrument_type
        })

    watchlist = PrivateWatchlist(
        name=name,
        group_name=group_name,
        watchlist_entries=watchlist_entries if watchlist_entries else None
    )

    await watchlist.a_upload(context.session)
    ctx.info(f"✅ Created private watchlist '{name}' with {len(watchlist_entries)} symbols")

@mcp.tool()
async def add_symbol_to_private_watchlist(
    ctx: Context,
    watchlist_name: str,
    symbol: str,
    instrument_type: Literal["Equity", "Equity Option", "Future", "Future Option", "Cryptocurrency", "Warrant"]
) -> None:
    context = get_context(ctx)
    watchlist = await PrivateWatchlist.a_get(context.session, watchlist_name)
    watchlist.add_symbol(symbol, instrument_type)
    await watchlist.a_update(context.session)
    ctx.info(f"✅ Added {symbol} to private watchlist '{watchlist_name}'")

@mcp.tool()
async def remove_symbol_from_private_watchlist(
    ctx: Context,
    watchlist_name: str,
    symbol: str,
    instrument_type: Literal["Equity", "Equity Option", "Future", "Future Option", "Cryptocurrency", "Warrant"]
) -> None:
    context = get_context(ctx)
    watchlist = await PrivateWatchlist.a_get(context.session, watchlist_name)
    watchlist.remove_symbol(symbol, instrument_type)
    await watchlist.a_update(context.session)
    ctx.info(f"✅ Removed {symbol} from private watchlist '{watchlist_name}'")

@mcp.tool()
async def delete_private_watchlist(ctx: Context, name: str) -> None:
    context = get_context(ctx)
    await PrivateWatchlist.a_remove(context.session, name)
    ctx.info(f"✅ Deleted private watchlist '{name}'")
