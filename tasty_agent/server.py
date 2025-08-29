import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta, datetime, date, timezone
from decimal import Decimal
import logging
import os
from typing import Literal, AsyncIterator, Any

import keyring
import humanize
from mcp.server.fastmcp import FastMCP, Context
from tastytrade import Session, Account
from tastytrade.dxfeed import Quote
from tastytrade.instruments import Equity, Option, a_get_option_chain
from tastytrade.market_sessions import a_get_market_sessions, a_get_market_holidays, ExchangeType, MarketStatus
from tastytrade.metrics import a_get_market_metrics
from tastytrade.order import NewOrder, OrderAction, OrderTimeInForce, OrderType
from tastytrade.search import a_symbol_search
from tastytrade.streamer import DXLinkStreamer
from tastytrade.watchlists import PublicWatchlist, PrivateWatchlist

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

    chain = await a_get_option_chain(session, symbol)
    target_date = datetime.strptime(expiration_date, "%Y-%m-%d").date()

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
    return {k: v for k, v in (await context.account.a_get_balances(context.session)).model_dump().items() if v is not None and v != 0}

@mcp.tool()
async def get_positions(ctx: Context) -> list[dict[str, Any]]:
    context = get_context(ctx)
    return [pos.model_dump() for pos in await context.account.a_get_positions(context.session, include_marks=True)]

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

        streamer_symbol = (await find_option_instrument(context.session, symbol, expiration_date, option_type, strike_price)).streamer_symbol
    else:
        streamer_symbol = symbol

    try:
        async with DXLinkStreamer(context.session) as streamer:
            await streamer.subscribe(Quote, [streamer_symbol])

            return (await asyncio.wait_for(streamer.get_event(Quote), timeout=timeout)).model_dump()

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
    return [h.model_dump() for h in await context.account.a_get_net_liquidating_value_history(context.session, time_back=time_back)]

@mcp.tool()
async def get_history(
    ctx: Context,
    start_date: str | None = None
) -> list[dict[str, Any]]:
    """start_date format: YYYY-MM-DD."""
    context = get_context(ctx)
    return [txn.model_dump() for txn in await context.account.a_get_history(context.session, start_date=date.today() - timedelta(days=90) if start_date is None else datetime.strptime(start_date, "%Y-%m-%d").date())]

@mcp.tool()
async def get_market_metrics(ctx: Context, symbols: list[str]) -> list[dict[str, Any]]:
    """
    Get market metrics including volatility (IV/HV), risk (beta, correlation),
    valuation (P/E, market cap), liquidity, dividends, earnings, and options data.
    """
    context = get_context(ctx)
    return [m.model_dump() for m in await a_get_market_metrics(context.session, symbols)]

@mcp.tool()
async def market_status(ctx: Context, exchanges: list[Literal['Equity', 'CME', 'CFE', 'Smalls']] = ['Equity']) -> list[dict[str, Any]]:
    """
    Get market status for each exchange including current open/closed state,
    next opening times, and holiday information.
    """
    context = get_context(ctx)
    market_sessions = await a_get_market_sessions(context.session, [ExchangeType(exchange) for exchange in exchanges])

    if not market_sessions:
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

@mcp.tool()
async def search_symbols(ctx: Context, symbol: str) -> list[dict[str, Any]]:
    """Search for symbols similar to the given search phrase."""
    context = get_context(ctx)
    return [result.model_dump() for result in await a_symbol_search(context.session, symbol)]

@mcp.tool()
async def get_live_orders(ctx: Context) -> list[dict[str, Any]]:
    context = get_context(ctx)
    return [order.model_dump() for order in await context.account.a_get_live_orders(context.session)]



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
        price: Limit price (absolute value - sign will be applied based on action)
        strike_price: Strike price (required for options)
        expiration_date: Expiration date in YYYY-MM-DD format (required for options)
        time_in_force: 'Day', 'GTC', or 'IOC'
        dry_run: If True, validates order without placing it

    Examples:
        Options: place_order("TQQQ", "C", "Buy", 17, 8.55, 100.0, "2026-01-16")
        Stock: place_order("AAPL", "Stock", "Buy", 100, 150.00)
    """
    context = get_context(ctx)

    if order_type in ['C', 'P']:
        if not strike_price or not expiration_date:
            raise ValueError(f"strike_price and expiration_date are required for {order_type} orders")
        
        instrument = await find_option_instrument(context.session, symbol, expiration_date, order_type, strike_price)
        order_action = OrderAction.BUY_TO_OPEN if action == 'Buy' else OrderAction.SELL_TO_CLOSE
    else:
        instrument = await Equity.a_get(context.session, symbol)
        order_action = OrderAction.BUY if action == 'Buy' else OrderAction.SELL

    order = NewOrder(
        time_in_force=OrderTimeInForce(time_in_force),
        order_type=OrderType.LIMIT,
        legs=[instrument.build_leg(Decimal(str(quantity)), order_action)],
        price=Decimal(str(-abs(price) if action == 'Buy' else abs(price)))
    )

    return (await context.account.a_place_order(context.session, order, dry_run=dry_run)).model_dump()

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
        return (await PublicWatchlist.a_get(context.session, name)).model_dump()
    else:
        return [watchlist.model_dump() for watchlist in await PublicWatchlist.a_get(context.session)]

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
        return (await PrivateWatchlist.a_get(context.session, name)).model_dump()
    else:
        return [w.model_dump() for w in await PrivateWatchlist.a_get(context.session)]


@mcp.tool()
async def add_symbol_to_private_watchlist(
    ctx: Context,
    symbol: str,
    instrument_type: Literal["Equity", "Equity Option", "Future", "Future Option", "Cryptocurrency", "Warrant"],
    name: str = "main"
) -> None:
    context = get_context(ctx)

    try:
        watchlist = await PrivateWatchlist.a_get(context.session, name)
        watchlist.add_symbol(symbol, instrument_type)
        await watchlist.a_update(context.session)
        ctx.info(f"✅ Added {symbol} to existing watchlist '{name}'")
    except Exception:
        watchlist = PrivateWatchlist(
            name=name,
            group_name="main",
            watchlist_entries=[{"symbol": symbol, "instrument_type": instrument_type}]
        )
        await watchlist.a_upload(context.session)
        ctx.info(f"✅ Created watchlist '{name}' and added {symbol}")

@mcp.tool()
async def remove_symbol_from_private_watchlist(
    ctx: Context,
    symbol: str,
    instrument_type: Literal["Equity", "Equity Option", "Future", "Future Option", "Cryptocurrency", "Warrant"],
    watchlist_name: str = "main"
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
