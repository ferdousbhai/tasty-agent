import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta, datetime, date
from decimal import Decimal
import keyring
import logging
import os
from typing import Literal, AsyncIterator, Any
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP, Context
from exchange_calendars import get_calendar
from tastytrade import Session, Account, metrics
from tastytrade.dxfeed import Quote
from tastytrade.instruments import NestedOptionChain, Equity, Option, Future, FutureOption, Cryptocurrency, Warrant
from tastytrade.order import NewOrder, OrderAction, OrderTimeInForce, OrderType, Leg
from tastytrade.streamer import DXLinkStreamer

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

    yield ServerContext(session=session, account=account)

mcp = FastMCP("TastyTrade", lifespan=lifespan)

async def build_order_legs(session: Session, legs_data: list[dict]) -> list[Leg]:
    """Helper function to build order legs from list of dictionaries."""
    order_legs = []
    for leg_data in legs_data:
        symbol = leg_data["symbol"]
        quantity = Decimal(str(leg_data["quantity"]))
        action = OrderAction(leg_data["action"])
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
async def get_balances(ctx: Context) -> dict[str, Any]:
    context = get_context(ctx)
    balances = await context.account.a_get_balances(context.session)
    return balances.model_dump()

@mcp.tool()
async def get_positions(ctx: Context) -> list[dict[str, Any]]:
    context = get_context(ctx)
    positions = await context.account.a_get_positions(context.session, include_marks=True)
    return [pos.model_dump() for pos in positions]

@mcp.tool()
async def get_live_orders(ctx: Context) -> list[dict[str, Any]]:
    context = get_context(ctx)
    orders = await context.account.a_get_live_orders(context.session)
    return [order.model_dump() for order in orders]

@mcp.tool()
async def get_option_chain(
    ctx: Context,
    underlying_symbol: str,
    expiration_date: str | None = None,
    min_strike: float | None = None,
    max_strike: float | None = None,
    option_type: Literal['C', 'P'] | None = None,
    max_results: int = 25
) -> dict[str, Any]:
    """Get filtered option chain data. expiration_date format: YYYY-MM-DD"""
    context = get_context(ctx)
    chains = await NestedOptionChain.a_get(context.session, underlying_symbol)
    if not chains:
        return {"error": "No option chain found", "underlying_symbol": underlying_symbol}

    chain = chains[0]

    target_exp = None
    if expiration_date:
        try:
            target_exp = datetime.strptime(expiration_date, "%Y-%m-%d").date()
        except ValueError:
            return {"error": "Invalid date format"}

    min_strike_decimal = Decimal(str(min_strike)) if min_strike else None
    max_strike_decimal = Decimal(str(max_strike)) if max_strike else None

    if option_type and option_type not in ['C', 'P']:
        return {"error": "option_type must be 'C' or 'P'"}

    result_exps = []
    for exp in chain.expirations:
        if target_exp and exp.expiration_date != target_exp:
            continue

        strikes = []
        for strike in exp.strikes[:max_results]:
            if min_strike_decimal and strike.strike_price < min_strike_decimal: continue
            if max_strike_decimal and strike.strike_price > max_strike_decimal: continue

            s = {"strike_price": float(strike.strike_price)}
            if option_type != 'P':
                s.update({"call": strike.call, "call_streamer_symbol": strike.call_streamer_symbol})
            if option_type != 'C':
                s.update({"put": strike.put, "put_streamer_symbol": strike.put_streamer_symbol})
            strikes.append(s)

        if strikes:
            result_exps.append({
                "expiration_date": exp.expiration_date.isoformat(),
                "expiration_type": exp.expiration_type,
                "days_to_expiration": exp.days_to_expiration,
                "settlement_type": exp.settlement_type,
                "strikes": strikes
            })

    return {
        "underlying_symbol": underlying_symbol,
        "root_symbol": chain.root_symbol,
        "option_chain_type": chain.option_chain_type,
        "shares_per_contract": chain.shares_per_contract,
        "expirations": result_exps
    }

@mcp.tool()
async def get_quote(ctx: Context, streamer_symbol: str, timeout: float = 10.0) -> dict[str, Any]:
    """Get live quote. Use streamer_symbol from get_option_chain."""
    context = get_context(ctx)
    try:
        async with DXLinkStreamer(context.session) as streamer:
            await streamer.subscribe(Quote, [streamer_symbol])
            quote = await asyncio.wait_for(streamer.get_event(Quote), timeout=timeout)
            return quote.model_dump()
    except asyncio.TimeoutError:
        raise ValueError(f"Timeout getting quote for {streamer_symbol}")

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
    metrics_data = await metrics.a_get_market_metrics(context.session, symbols)
    return [m.model_dump() for m in metrics_data]

@mcp.tool()
async def check_market_status(ctx: Context) -> dict[str, Any]:
    nyse = get_calendar('XNYS')
    current_time = datetime.now(ZoneInfo('America/New_York'))
    is_open = nyse.is_open_on_minute(current_time)

    result = {
        "is_open": is_open,
        "current_time": current_time.isoformat()
    }

    if not is_open:
        next_open = nyse.next_open(current_time)
        result["next_open"] = next_open.isoformat()
        result["time_until_open_seconds"] = (next_open - current_time).total_seconds()

    return result

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
    Actions: Equity: "Buy"/"Sell". Options: "Buy to Open"/"Sell to Close"/"Buy to Close"/"Sell to Open".
    Instrument type: "Equity", "Equity Option", "Cryptocurrency" etc
    Price: negative for debits, positive for credits. Option symbols auto-normalized to OSI format.
    """
    context = get_context(ctx)
    order_legs = await build_order_legs(context.session, legs)

    order = NewOrder(
        time_in_force=OrderTimeInForce(time_in_force),
        order_type=OrderType(order_type),
        legs=order_legs,
        price=Decimal(str(price)) if price is not None else None
    )

    response = await context.account.a_place_order(context.session, order, dry_run=dry_run)

    return {
        "order": response.order.model_dump() if response.order else None,
        "dry_run": dry_run,
        "errors": [str(e) for e in response.errors] if response.errors else [],
        "warnings": [str(w) for w in response.warnings] if response.warnings else []
    }

@mcp.tool()
async def delete_order(ctx: Context, order_id: str) -> dict[str, Any]:
    context = get_context(ctx)
    await context.account.a_delete_order(context.session, int(order_id))
    return {"success": True, "order_id": order_id}

@mcp.tool()
async def get_order(ctx: Context, order_id: str) -> dict[str, Any]:
    context = get_context(ctx)
    order = await context.account.a_get_order(context.session, int(order_id))
    return order.model_dump()

@mcp.tool()
async def replace_order(
    ctx: Context,
    order_id: str,
    legs: list[dict] | None = None,
    price: float | None = None,
    dry_run: bool = False
) -> dict[str, Any]:
    """Modify existing order. legs: same format as place_order or None to keep existing."""
    context = get_context(ctx)
    order_to_modify = await context.account.a_get_order(context.session, int(order_id))

    if legs:
        order_to_modify.legs = await build_order_legs(context.session, legs)

    if price is not None:
        order_to_modify.price = Decimal(str(price))

    response = await context.account.a_replace_order(context.session, int(order_id), order_to_modify, dry_run=dry_run)

    return {
        "old_order_id": order_id,
        "new_order": response.order.model_dump() if response.order else None,
        "dry_run": dry_run,
        "errors": [str(e) for e in response.errors] if response.errors else [],
        "warnings": [str(w) for w in response.warnings] if response.warnings else []
    }