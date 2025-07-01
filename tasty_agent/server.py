import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta, datetime, date, timezone
from decimal import Decimal
import keyring
import logging
import os
from typing import Literal, AsyncIterator, Any

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

def normalize_occ_symbol(symbol: str) -> str:
    """Normalize OCC symbols to OSI format: RRRRRRYYMMDDCPPPPPPPPP (21 chars total)"""
    clean_symbol = symbol.replace(" ", "")

    if len(clean_symbol) < 15:
        raise ValueError(f"Invalid OCC symbol format: {symbol}")

    # Extract components from end backwards
    strike = clean_symbol[-8:]
    call_put = clean_symbol[-9]
    if call_put not in ['C', 'P']:
        raise ValueError(f"Invalid call/put indicator in symbol: {symbol}")

    expiration = clean_symbol[-15:-9]
    if len(expiration) != 6 or not expiration.isdigit():
        raise ValueError(f"Invalid expiration format in symbol: {symbol}")

    root = clean_symbol[:-15]
    if len(root) == 0 or len(root) > 6:
        raise ValueError(f"Invalid root symbol length in symbol: {symbol}")

    return f"{root.ljust(6)}{expiration}{call_put}{strike}"

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
async def get_option_streamer_symbols(
    ctx: Context,
    underlying_symbol: str,
    expiration_date: str,
    min_strike_price: float,
    max_strike_price: float,
    option_type: Literal['C', 'P']
) -> list[str]:
    """Get filtered option chain data. expiration_date format: YYYY-MM-DD"""
    context = get_context(ctx)
    chains = await NestedOptionChain.a_get(context.session, underlying_symbol)
    if not chains:
        raise ValueError(f"No option chain found for {underlying_symbol}")

    chain = chains[0]

    target_exp = datetime.strptime(expiration_date, "%Y-%m-%d").date()

    min_strike_decimal = Decimal(str(min_strike_price))
    max_strike_decimal = Decimal(str(max_strike_price))

    if option_type not in ['C', 'P']:
        raise ValueError("option_type must be 'C' or 'P'")

    streamer_symbols = []
    for option_expiration in chain.expirations:
        if option_expiration.expiration_date != target_exp:
            continue

        for strike in option_expiration.strikes:
            if strike.strike_price < min_strike_decimal: continue
            if strike.strike_price > max_strike_decimal: continue

            if option_type == 'C':
                streamer_symbols.append(strike.call_streamer_symbol)
            else:  # option_type == 'P'
                streamer_symbols.append(strike.put_streamer_symbol)

    return streamer_symbols

@mcp.tool()
async def get_quote(ctx: Context, streamer_symbol: str, timeout: float = 10.0) -> dict[str, Any]:
    """Get live quote for a ticker symbol. For options, use streamer_symbol from get_option_chain."""
    context = get_context(ctx)

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

async def build_order_legs(session: Session, legs_data: list[dict]) -> list[Leg]:
    """Helper function to build order legs from list of dictionaries."""
    order_legs = []
    for leg_data in legs_data:
        symbol = leg_data["symbol"]
        quantity = Decimal(str(leg_data["quantity"]))

        # Validate action is a valid OrderAction value
        action_str = leg_data["action"]
        if action_str not in [action.value for action in OrderAction]:
            valid_actions = [action.value for action in OrderAction]
            raise ValueError(f"Invalid action '{action_str}'. Valid actions: {valid_actions}")
        action = OrderAction(action_str)

        instrument_type = leg_data["instrument_type"]

        if instrument_type == "Equity":
            instrument = await Equity.a_get(session, symbol)
            leg = instrument.build_leg(quantity, action)
        elif instrument_type == "Equity Option":
            normalized_symbol = normalize_occ_symbol(symbol)
            instrument = await Option.a_get(session, normalized_symbol)
            leg = instrument.build_leg(quantity, action)
        elif instrument_type == "Future":
            instrument = await Future.a_get(session, symbol)
            leg = instrument.build_leg(quantity, action)
        elif instrument_type == "Future Option":
            instrument = await FutureOption.a_get(session, symbol)
            leg = instrument.build_leg(quantity, action)
        elif instrument_type == "Cryptocurrency":
            instrument = await Cryptocurrency.a_get(session, symbol)
            leg = instrument.build_leg(quantity, action)
        elif instrument_type == "Warrant":
            instrument = await Warrant.a_get(session, symbol)
            leg = instrument.build_leg(quantity, action)
        else:
            raise ValueError(f"Unsupported instrument type: {instrument_type}")

        order_legs.append(leg)

    return order_legs

@mcp.tool()
async def place_order(
    ctx: Context,
    legs: list[dict],
    order_type: Literal['Limit', 'Market'] = "Limit",
    time_in_force: Literal['Day', 'GTC', 'IOC'] = "Day",
    price: float | None = None,
    dry_run: bool = False
) -> dict[str, Any]:
    """
    Place order. legs: List of dicts with symbol, quantity, action, instrument_type.
    Actions: "Buy to Open"/"Sell to Close"/"Buy to Close"/"Sell to Open", "Buy"/"Sell" (futures only).
    Instrument type: "Equity", "Equity Option", "Cryptocurrency" etc
    Price: negative for debits, positive for credits. Option symbols auto-normalized to OSI format.
    Strategy: Try mid-price first, check execution with get_live_orders, revise price if unfilled.
    """
    context = get_context(ctx)

    # Validate order_type and time_in_force are valid enum values
    if order_type not in [ot.value for ot in OrderType]:
        valid_types = [ot.value for ot in OrderType]
        raise ValueError(f"Invalid order_type '{order_type}'. Valid types: {valid_types}")

    if time_in_force not in [tif.value for tif in OrderTimeInForce]:
        valid_tifs = [tif.value for tif in OrderTimeInForce]
        raise ValueError(f"Invalid time_in_force '{time_in_force}'. Valid values: {valid_tifs}")

    order_legs = await build_order_legs(context.session, legs)
    order = NewOrder(
        time_in_force=OrderTimeInForce(time_in_force),
        order_type=OrderType(order_type),
        legs=order_legs,
        price=Decimal(str(price)) if price is not None else None
    )
    response = await context.account.a_place_order(context.session, order, dry_run=dry_run)
    return response.model_dump()

@mcp.tool()
async def delete_order(ctx: Context, order_id: str) -> dict[str, Any]:
    context = get_context(ctx)
    await context.account.a_delete_order(context.session, int(order_id))
    return {"success": True, "order_id": order_id}

@mcp.tool()
async def get_public_watchlist_names(ctx: Context) -> list[str]:
    """Use this to get the name of a watchlist to use with get_public_watchlist_entry_symbols()."""
    context = get_context(ctx)
    watchlists = await PublicWatchlist.a_get(context.session)
    return [watchlist.name for watchlist in watchlists]

@mcp.tool()
async def get_public_watchlist_entries(ctx: Context, name: str) -> list[dict]:
    """Use get_public_watchlist_names() first to see available watchlist names."""
    context = get_context(ctx)
    watchlist = await PublicWatchlist.a_get(context.session, name)
    return watchlist.watchlist_entries

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

@mcp.tool()
async def create_private_watchlist(
    ctx: Context,
    name: str,
    entries: list[dict] = [],
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