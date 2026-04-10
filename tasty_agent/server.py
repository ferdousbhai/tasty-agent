import logging
from datetime import UTC, datetime
from typing import Any, Literal

from aiolimiter import AsyncLimiter
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.prompts.base import AssistantMessage, Message, UserMessage
from tastytrade import Session, metrics
from tastytrade.dxfeed import Greeks, Quote, Summary, Trade
from tastytrade.market_sessions import ExchangeType, get_market_holidays
from tastytrade.order import OrderTimeInForce
from tastytrade.search import symbol_search
from tastytrade.utils import now_in_new_york

from tasty_agent.account_helpers import build_account_overview, fetch_history
from tasty_agent.core import ServerContext, get_context, get_session, lifespan, to_table
from tasty_agent.market_data import (
    exchanges_for_symbols as _exchanges_for_symbols,
    get_next_open_time as _get_next_open_time,
    stream_events as _stream_events,
    stream_multi_events as _stream_multi_events,
    stream_quotes_with_trade_fallback as _stream_quotes_with_trade_fallback,
)
from tasty_agent.orders import (
    InstrumentDetail,
    InstrumentSpec,
    OrderLeg,
    _option_chain_key_builder,
    build_new_order,
    build_order_legs,
    describe_instrument,
    find_live_order,
    get_instrument_details,
    validate_date_format,
    validate_strike_price,
)
from tasty_agent.watchlists import WatchlistSymbol, manage_watchlist

logger = logging.getLogger(__name__)

rate_limiter = AsyncLimiter(2, 1) # 2 requests per second

mcp_app = FastMCP("TastyTrade", lifespan=lifespan)


def main() -> None:
    """CLI entry point — accepts optional transport argument (stdio, sse, streamable-http)."""
    import sys
    valid = ("stdio", "sse", "streamable-http")
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    if transport not in valid:
        print(f"Invalid transport '{transport}'. Must be one of: {', '.join(valid)}", file=sys.stderr)
        sys.exit(1)
    mcp_app.run(transport)  # type: ignore[arg-type]  # validated above

async def calculate_net_price(ctx: Context, instrument_details: list[InstrumentDetail], legs: list[OrderLeg]) -> float:
    """Calculate net price from current market quotes."""
    session = get_session(ctx)
    quotes = await _stream_events(session, Quote, [d.streamer_symbol for d in instrument_details], timeout=10.0)

    net_price = 0.0
    for quote, detail, leg in zip(quotes, instrument_details, legs, strict=True):
        if quote.bid_price is not None and quote.ask_price is not None:
            mid_price = float(quote.bid_price + quote.ask_price) / 2
            leg_price = -mid_price if leg.action.startswith('Buy') else mid_price
            net_price += leg_price * leg.quantity
        else:
            symbol_info = describe_instrument(detail)
            logger.warning(f"Could not get bid/ask prices for {symbol_info}")
            raise ValueError(f"Could not get bid/ask for {symbol_info}")

    return round(net_price * 100) / 100


async def _resolve_order_inputs(
    ctx: Context,
    legs: list[OrderLeg],
    price: float | None,
) -> tuple[list[InstrumentDetail], float]:
    """Resolve instruments and determine the order price."""
    session = get_session(ctx)
    instrument_specs = [leg.to_instrument_spec() for leg in legs]
    instrument_details = await get_instrument_details(session, instrument_specs)

    if price is not None:
        return instrument_details, price

    try:
        resolved_price = await calculate_net_price(ctx, instrument_details, legs)
        await ctx.info(f"💰 Auto-calculated net mid-price: ${resolved_price:.2f}")
        logger.info(f"Auto-calculated price ${resolved_price:.2f} for {len(legs)}-leg order")
        return instrument_details, resolved_price
    except Exception as e:
        logger.warning(f"Failed to auto-calculate price for order legs {[leg.symbol for leg in legs]}: {e!s}")
        raise ValueError(f"Could not fetch quotes for price calculation: {e!s}. Please provide a price.") from e


async def _place_new_order(
    ctx: Context,
    legs: list[OrderLeg],
    time_in_force: OrderTimeInForce,
    price: float | None,
    dry_run: bool,
) -> dict[str, Any]:
    """Resolve a new order and place it."""
    context = get_context(ctx)
    instrument_details, resolved_price = await _resolve_order_inputs(ctx, legs, price)
    order = build_new_order(
        time_in_force=time_in_force,
        legs=build_order_legs(instrument_details, legs),
        price=resolved_price,
    )
    return (await context.account.place_order(context.session, order, dry_run=dry_run)).model_dump()


async def _find_live_order(ctx: Context, order_id: str):
    """Return a live order by id, raising a helpful error if missing."""
    context = get_context(ctx)
    return await find_live_order(context.account, context.session, order_id)



@mcp_app.tool()
async def account_overview(
    ctx: Context,
    include: list[Literal["balances", "positions"]] | None = None,
) -> dict[str, Any]:
    """
    Get account balances and/or open positions.

    Args:
        include: Sections to return (default: ["balances", "positions"]).

    Balances include net_liquidating_value, cash_balance, buying_power, margin requirements, etc.
    Use get_history(type="transactions", transaction_type="Money Movement") to separate
    trading performance from deposits/withdrawals.
    """
    return await build_account_overview(ctx, include)


@mcp_app.tool()
async def get_history(
    ctx: Context,
    type: Literal["transactions", "orders"],
    days: int | None = None,
    underlying_symbol: str | None = None,
    transaction_type: Literal["Trade", "Money Movement"] | None = None,
    page_offset: int = 0,
    max_results: int = 50,
) -> str:
    """
    Get transaction or order history (paginated).

    Args:
        type: "transactions" for trade/cash flow history, "orders" for filled/canceled/rejected orders.
        days: Number of days to look back (default: 90 for transactions, 7 for orders).
        underlying_symbol: Filter by underlying symbol.
        transaction_type: Filter transactions by type (only for type="transactions").
        page_offset: Starting offset for pagination (default: 0).
        max_results: Maximum number of results to return (default: 50). Use with page_offset to paginate through large result sets.
    """
    async with rate_limiter:
        return await fetch_history(
            ctx,
            type=type,
            days=days,
            underlying_symbol=underlying_symbol,
            transaction_type=transaction_type,
            page_offset=page_offset,
            max_results=max_results,
        )


@mcp_app.tool()
async def manage_order(
    ctx: Context,
    action: Literal["place", "replace", "cancel", "list"],
    legs: list[OrderLeg] | None = None,
    price: float | None = None,
    time_in_force: OrderTimeInForce = OrderTimeInForce.DAY,
    dry_run: bool = False,
    order_id: str | None = None,
) -> dict[str, Any] | str:
    """
    Place, replace, cancel, or list orders.

    Actions:
        list: Get all live (active) orders. No other params needed.
        place: Place a new order. Requires 'legs'. Price auto-calculated from mid-quote if omitted.
               For debit orders use negative price (e.g., -8.50), for credit use positive (e.g., 2.25).
               If Tastytrade returns a price granularity error, retry rounded to nearest 5 cents.
        replace: Modify an existing order's price. Requires 'order_id' and 'price'.
                 For complex changes (different legs/quantities), cancel and place a new order.
        cancel: Cancel an existing order. Requires 'order_id'.

    Examples:
        List: manage_order("list")
        Auto-priced stock: manage_order("place", legs=[{"symbol": "AAPL", "action": "Buy to Open", "quantity": 100}])
        Manual-priced option: manage_order("place", legs=[{"symbol": "TQQQ", "option_type": "C", "action": "Buy to Open", "quantity": 17, "strike_price": 100.0, "expiration_date": "2026-01-16"}], price=-8.50)
        Future: manage_order("place", legs=[{"symbol": "/ESM26", "action": "Buy", "quantity": 1}], price=-10.0)
        Replace: manage_order("replace", order_id="12345", price=-10.05)
        Cancel: manage_order("cancel", order_id="12345")
    """
    context = get_context(ctx)
    session = context.session

    if action == "list":
        orders = await context.account.get_live_orders(session)
        return to_table(orders)

    if action == "place":
        if not legs:
            raise ValueError("'legs' is required for action='place'")

        async with rate_limiter:
            return await _place_new_order(ctx, legs, time_in_force, price, dry_run)

    if not order_id:
        raise ValueError(f"'order_id' is required for action='{action}'")

    if action == "replace":
        if price is None:
            raise ValueError("'price' is required for action='replace'")

        async with rate_limiter:
            existing_order = await _find_live_order(ctx, order_id)
            return (await context.account.replace_order(
                session,
                int(order_id),
                build_new_order(
                    time_in_force=existing_order.time_in_force,
                    legs=existing_order.legs,
                    price=price,
                )
            )).model_dump()

    await context.account.delete_order(session, int(order_id))
    return {"success": True, "order_id": order_id}


@mcp_app.tool()
async def get_quotes(
    ctx: Context,
    instruments: list[InstrumentSpec],
    timeout: float = 10.0
) -> str:
    """
    Get live quotes for stocks, options, futures, and indices via DXLink streaming.

    Args:
        instruments: List of instrument specifications. Each contains:
            - symbol: str - Symbol (e.g., 'AAPL', '/ESH26', 'SPX')
            - instrument_type: str - Optional. Auto-detected if omitted ('/' prefix → Future).
              Use 'Index' for index symbols (SPX, VIX, NDX).
            - option_type: 'C' or 'P' (optional, omit for stocks/futures/indices)
            - strike_price: float (required for options)
            - expiration_date: str - YYYY-MM-DD format (required for options)
        timeout: Timeout in seconds

    Examples:
        Stock: get_quotes([{"symbol": "AAPL"}])
        Index: get_quotes([{"symbol": "SPX", "instrument_type": "Index"}])
        Option: get_quotes([{"symbol": "TQQQ", "option_type": "C", "strike_price": 100.0, "expiration_date": "2026-01-16"}])
    """
    if not instruments:
        raise ValueError("At least one instrument is required")

    session = get_session(ctx)
    instrument_details = await get_instrument_details(session, instruments)
    streamer_symbols = [d.streamer_symbol for d in instrument_details]
    index_symbols = {d.streamer_symbol for d in instrument_details if d.is_index}

    if index_symbols:
        events = await _stream_quotes_with_trade_fallback(session, streamer_symbols, index_symbols, timeout)
    else:
        events = await _stream_events(session, Quote, streamer_symbols, timeout)
    return to_table(events)


@mcp_app.tool()
async def get_greeks(
    ctx: Context,
    options: list[InstrumentSpec],
    timeout: float = 10.0
) -> str:
    """
    Get Greeks (delta, gamma, theta, vega, rho) for options via DXLink streaming.

    Args:
        options: List of option specifications. Each contains:
            - symbol: str - Stock symbol (e.g., 'AAPL', 'TQQQ')
            - option_type: 'C' or 'P'
            - strike_price: float
            - expiration_date: str - YYYY-MM-DD format
        timeout: Timeout in seconds
    """
    if not options:
        raise ValueError("At least one option is required")

    session = get_session(ctx)
    option_details = await get_instrument_details(session, options)

    greeks = await _stream_events(session, Greeks, [d.streamer_symbol for d in option_details], timeout)
    return to_table(greeks)


@mcp_app.tool()
async def get_gex(
    ctx: Context,
    symbol: str,
    expiration_date: str,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """
    Get Gamma Exposure (GEX) analysis for an option chain.

    Computes dealer gamma exposure per strike (gamma × OI × 100) and returns
    key levels: net GEX, gamma regime, flip level, call/put walls, and top strikes.

    Assumes standard dealer positioning: long calls (positive GEX), short puts (negative GEX).

    Args:
        symbol: Underlying symbol (e.g., 'SPY', 'SPX', 'AAPL').
        expiration_date: Expiration date in YYYY-MM-DD format.
        timeout: Timeout in seconds (default: 60). Large chains may need more time.
    """
    session = get_session(ctx)
    target_date = validate_date_format(expiration_date)

    chain = await get_cached_option_chain(session, symbol.upper())
    if target_date not in chain:
        available_dates = sorted(chain.keys())
        raise ValueError(
            f"No options for {symbol.upper()} expiration {expiration_date}. "
            f"Available: {available_dates}"
        )

    options: list[Option] = chain[target_date]
    streamer_symbols = [opt.streamer_symbol for opt in options]

    if not streamer_symbols:
        raise ValueError(f"No option contracts found for {symbol.upper()} {expiration_date}")

    opt_by_sym = {opt.streamer_symbol: opt for opt in options}

    # Stream Greeks + Summary concurrently on a single DXLink connection
    data = await _stream_multi_events(session, [Greeks, Summary], streamer_symbols, timeout)
    greeks_map = data[Greeks]
    summary_map = data[Summary]

    # Compute per-strike GEX: gamma × OI × 100 (positive for calls, negative for puts)
    strike_gex: dict[float, float] = {}
    for sym, opt in opt_by_sym.items():
        gamma_event = greeks_map.get(sym)
        summary_event = summary_map.get(sym)
        if not gamma_event or not summary_event:
            continue
        gamma = float(gamma_event.gamma) if gamma_event.gamma is not None else 0.0
        oi = summary_event.open_interest or 0
        gex = gamma * oi * 100
        strike = float(opt.strike_price)
        # Calls: positive (dealers long), Puts: negative (dealers short)
        if opt.option_type.value == "P":
            gex = -gex
        strike_gex[strike] = strike_gex.get(strike, 0.0) + gex

    if not strike_gex:
        raise ValueError(f"No GEX data available for {symbol.upper()} {expiration_date}")

    net_gex = sum(strike_gex.values())

    # GEX flip level: strike where cumulative GEX (ascending) crosses zero
    sorted_strikes = sorted(strike_gex.items())
    gex_flip_level = None
    cumulative = 0.0
    for i, (strike, gex) in enumerate(sorted_strikes):
        prev_cumulative = cumulative
        cumulative += gex
        if i > 0 and prev_cumulative * cumulative < 0:
            # Linear interpolation between the two strikes
            prev_strike = sorted_strikes[i - 1][0]
            gex_flip_level = prev_strike + (strike - prev_strike) * (-prev_cumulative / (cumulative - prev_cumulative))
            break

    # Call wall: strike with highest positive GEX
    positive_strikes = {s: g for s, g in strike_gex.items() if g > 0}
    call_wall = max(positive_strikes, key=positive_strikes.get) if positive_strikes else None

    # Put wall: strike with most negative GEX
    negative_strikes = {s: g for s, g in strike_gex.items() if g < 0}
    put_wall = min(negative_strikes, key=negative_strikes.get) if negative_strikes else None

    # Top strikes by absolute GEX
    top = sorted(strike_gex.items(), key=lambda x: abs(x[1]), reverse=True)[:5]

    result: dict[str, Any] = {
        "net_gex": round(net_gex, 2),
        "gamma_regime": "positive" if net_gex >= 0 else "negative",
        "gex_flip_level": round(gex_flip_level, 2) if gex_flip_level is not None else None,
    }
    if call_wall is not None:
        result["call_wall"] = {"strike": call_wall, "gex": round(positive_strikes[call_wall], 2)}
    if put_wall is not None:
        result["put_wall"] = {"strike": put_wall, "gex": round(negative_strikes[put_wall], 2)}
    result["top_strikes"] = [{"strike": s, "gex": round(g, 2)} for s, g in top]

    return result


@mcp_app.tool()
async def get_market_metrics(ctx: Context, symbols: list[str]) -> str:
    """
    Get market metrics including volatility (IV/HV), risk (beta, correlation),
    valuation (P/E, market cap), liquidity, dividends, earnings, and options data.

    Note extreme IV rank/percentile (0-1): low = cheap options (buy opportunity), high = expensive options (close positions).
    """
    session = get_session(ctx)
    result = await metrics.get_market_metrics(session, symbols)
    return to_table(result)


@mcp_app.tool()
async def market_status(ctx: Context, exchanges: list[Literal['Equity', 'CME', 'CFE', 'Smalls']] | None = None) -> dict[str, Any]:
    """
    Get market status for each exchange including current open/closed state,
    next opening times, holiday information, and current NYC time.
    """
    if exchanges is None:
        exchanges = ['Equity']
    session = get_session(ctx)
    market_sessions = await get_market_sessions(session, [ExchangeType(exchange) for exchange in exchanges])

    if not market_sessions:
        raise ValueError(f"No market sessions found for exchanges: {exchanges}")

    current_time = datetime.now(UTC)
    calendar = await get_market_holidays(session)
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

    return {"current_time_nyc": now_in_new_york().isoformat(), "exchanges": results}


@mcp_app.tool()
async def search_symbols(ctx: Context, symbol: str, max_results: int = 20) -> str:
    """Search for symbols similar to the given search phrase.

    Args:
        symbol: Search phrase (e.g., 'AAPL', 'Apple').
        max_results: Maximum number of results to return (default: 20).
    """
    session = get_session(ctx)
    async with rate_limiter:
        results = await symbol_search(session, symbol)
    return to_table(results[:max_results])


@mcp_app.tool()
async def watchlist(
    ctx: Context,
    action: Literal["list", "add", "remove", "delete"],
    watchlist_type: Literal['public', 'private'] = 'private',
    name: str | None = None,
    symbols: list[WatchlistSymbol] | None = None,
) -> list[dict[str, Any]] | dict[str, Any]:
    """
    Manage watchlists: list, add symbols, remove symbols, or delete.

    Actions:
        list: Get watchlists. No name = all watchlists. With name = specific watchlist.
              Use watchlist_type to switch between 'public' and 'private'.
        add: Add symbols to a private watchlist (creates it if it doesn't exist).
        remove: Remove symbols from a private watchlist.
        delete: Delete a private watchlist by name.

    Args:
        symbols: Required for 'add' and 'remove'. Each symbol has:
            - symbol: str - e.g., "AAPL", "TSLA"
            - instrument_type: str - "Equity", "Equity Option", "Future", etc.
    """
    return await manage_watchlist(ctx, action, watchlist_type, name, symbols)



@mcp_app.prompt(title="IV Rank Analysis")
def analyze_iv_opportunities() -> list[Message]:
    return [
        UserMessage("""Please analyze IV rank, percentile, and liquidity for:
1. All active positions in my account
2. All symbols in my watchlists

Focus on identifying extremes:
- Low IV rank (<.2) may present entry opportunities (cheap options)
- High IV rank (>.8) may present exit opportunities (expensive options)
- Also consider liquidity levels to ensure tradeable positions

Use the account_overview, watchlist, and get_market_metrics tools to gather this data."""),
        AssistantMessage("""I'll analyze IV opportunities for your positions and watchlist. Let me start by gathering your current positions and watchlist data, then get market metrics for each symbol to assess IV rank extremes and liquidity.""")
    ]
