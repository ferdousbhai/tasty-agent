import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from html import escape as escape_xml_text
from typing import Any, Literal

import humanize
from aiolimiter import AsyncLimiter
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.prompts.base import AssistantMessage, Message, UserMessage
from tastytrade import metrics
from tastytrade.dxfeed import Greeks, Quote
from tastytrade.market_sessions import ExchangeType, MarketStatus, get_market_holidays, get_market_sessions
from tastytrade.order import OrderTimeInForce
from tastytrade.search import symbol_search
from tastytrade.utils import now_in_new_york

from tasty_agent.account_helpers import build_account_overview, fetch_history
from tasty_agent.core import compact_row, compact_value, get_context, get_session, lifespan, to_table
from tasty_agent.market_data import (
    get_next_open_time as _get_next_open_time,
)
from tasty_agent.market_data import (
    stream_events as _stream_events,
)
from tasty_agent.market_data import (
    stream_quotes_with_trade_fallback as _stream_quotes_with_trade_fallback,
)
from tasty_agent.orders import (
    InstrumentDetail,
    InstrumentSpec,
    OptionSpec,
    OrderLeg,
    OrderSizingPolicy,
    OrderSizingResult,
    apply_order_sizing,
    build_new_order,
    build_order_legs,
    build_order_market,
    default_pricing_policy,
    find_live_order,
    format_order_market,
    format_signed_money,
    get_instrument_details,
    get_order_leg_instrument_details,
    resolve_order_price,
)
from tasty_agent.watchlists import WatchlistSymbol, manage_watchlist

logger = logging.getLogger(__name__)

rate_limiter = AsyncLimiter(2, 1)  # 2 requests per second

mcp_app = FastMCP("TastyTrade", lifespan=lifespan)

TOOL_XML_TAGS = {
    "get_history": "history",
    "place_order": "order",
    "replace_order": "order",
    "cancel_order": "order",
    "list_orders": "orders",
    "get_quotes": "quotes",
    "get_greeks": "greeks",
    "get_market_metrics": "market_metrics",
    "search_symbols": "symbol_search",
}


def tool_xml(tool_name: str, payload: Any, *, error: bool = False) -> str:
    """Format MCP tool output as one concise XML block."""
    tag_name = TOOL_XML_TAGS.get(tool_name, tool_name)
    attrs = ' error="true"' if error else ""
    text = payload if isinstance(payload, str) else json.dumps(payload, default=str, separators=(",", ":"))
    return f"<{tag_name}{attrs}>{escape_xml_text(text, quote=False)}</{tag_name}>"


def main() -> None:
    """CLI entry point — accepts optional transport argument (stdio, sse, streamable-http)."""
    import sys

    valid = ("stdio", "sse", "streamable-http")
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    if transport not in valid:
        print(f"Invalid transport '{transport}'. Must be one of: {', '.join(valid)}", file=sys.stderr)
        sys.exit(1)
    mcp_app.run(transport)  # type: ignore[arg-type]  # validated above


async def _fetch_order_market(ctx: Context, instrument_details: list[InstrumentDetail], legs: list[Any]):
    """Fetch current quotes and build the signed net market for order legs."""
    session = get_session(ctx)
    quotes = await _stream_events(session, Quote, [d.streamer_symbol for d in instrument_details], timeout=10.0)
    return build_order_market(instrument_details, legs, quotes)


async def _resolve_order_inputs(
    ctx: Context,
    legs: list[OrderLeg],
    target_value: float | None,
) -> tuple[list[InstrumentDetail], list[OrderLeg], Decimal, OrderSizingResult | None]:
    """Resolve instruments and determine the order price."""
    session = get_session(ctx)
    instrument_specs = [leg.to_instrument_spec() for leg in legs]
    instrument_details = await get_instrument_details(session, instrument_specs)

    try:
        pricing_policy = default_pricing_policy()
        market = await _fetch_order_market(ctx, instrument_details, legs)
        resolved_price, warnings = resolve_order_price(market, pricing_policy)
        sizing_policy = None
        if target_value is not None:
            sizing_policy = OrderSizingPolicy(
                target_value=Decimal(str(target_value)),
                min_quantity=1,
                max_quantity=None,
            )
        sized_legs, sizing_result = apply_order_sizing(instrument_details, legs, resolved_price, sizing_policy)
        for warning in warnings:
            await ctx.warning(warning)
        await ctx.info(
            f"Resolved limit price {format_signed_money(resolved_price)} "
            f"from mid ({format_order_market(market)})."
        )
        logger.info(f"Auto-calculated price {resolved_price} for {len(legs)}-leg order")
        if sizing_result is not None:
            await ctx.info(
                f"Sized order to {sizing_result.quantity} unit(s): "
                f"${sizing_result.estimated_value.quantize(Decimal('0.01'))} estimated value "
                f"from ${sizing_result.target_value} target."
            )
        return instrument_details, sized_legs, resolved_price, sizing_result
    except Exception as e:
        logger.warning(f"Failed to resolve safe price for order legs {[leg.symbol for leg in legs]}: {e!s}")
        raise ValueError(f"Could not resolve a safe limit price from live quotes: {e!s}") from e


async def _place_new_order(
    ctx: Context,
    legs: list[OrderLeg],
    time_in_force: OrderTimeInForce,
    target_value: float | None,
    dry_run: bool,
) -> dict[str, Any]:
    """Resolve a new order and place it."""
    context = get_context(ctx)
    instrument_details, sized_legs, resolved_price, sizing_result = await _resolve_order_inputs(
        ctx,
        legs,
        target_value,
    )
    order = build_new_order(
        time_in_force=time_in_force,
        legs=build_order_legs(instrument_details, sized_legs),
        price=resolved_price,
    )
    response = await context.account.place_order(context.session, order, dry_run=dry_run)
    result = _compact_order_response(response)
    compact_sizing = _compact_sizing_result(sizing_result)
    if compact_sizing:
        result["sizing"] = compact_sizing
    return result


async def _find_live_order(ctx: Context, order_id: str):
    """Return a live order by id, raising a helpful error if missing."""
    context = get_context(ctx)
    return await find_live_order(context.account, context.session, order_id)


async def _resolve_replacement_price(
    ctx: Context,
    broker_legs: list[Any],
) -> Decimal:
    """Resolve and validate a replacement order price against existing live order legs."""
    if not broker_legs:
        raise ValueError("Cannot replace an order without live order legs.")

    session = get_session(ctx)
    instrument_details = await get_order_leg_instrument_details(session, broker_legs)
    market = await _fetch_order_market(ctx, instrument_details, broker_legs)
    return await _resolve_replacement_market_price(ctx, market)


async def _resolve_replacement_market_price(
    ctx: Context,
    market,
) -> Decimal:
    pricing_policy = default_pricing_policy()
    resolved_price, warnings = resolve_order_price(market, pricing_policy)
    for warning in warnings:
        await ctx.warning(warning)
    await ctx.info(
        f"Resolved replacement limit {format_signed_money(resolved_price)} "
        f"from mid ({format_order_market(market)})."
    )
    return resolved_price


def _compact_order_legs(legs: list[Any] | None) -> str | None:
    if not legs:
        return None
    parts = []
    for leg in legs:
        action = compact_value(getattr(leg, "action", None))
        quantity = compact_value(getattr(leg, "quantity", None))
        symbol = getattr(leg, "symbol", None)
        if action and quantity and symbol:
            parts.append(f"{action} {quantity} {symbol}")
        elif symbol:
            parts.append(str(symbol))
    return "; ".join(parts) if parts else None


def _compact_order(order) -> dict[str, Any]:
    data = order.model_dump()
    row = {
        "id": compact_value(data.get("id")),
        "status": compact_value(data.get("status")),
        "underlying": compact_value(data.get("underlying_symbol")),
        "type": compact_value(data.get("order_type")),
        "tif": compact_value(data.get("time_in_force")),
        "price": compact_value(data.get("price")),
        "size": compact_value(data.get("size")),
        "legs": _compact_order_legs(getattr(order, "legs", None)),
        "received_at": compact_value(data.get("received_at")),
        "updated_at": compact_value(data.get("updated_at")),
        "reject_reason": compact_value(data.get("reject_reason")),
    }
    return compact_row(row, drop_zero_string=True)


def _compact_sizing_result(sizing_result: OrderSizingResult | None) -> dict[str, Any] | None:
    if sizing_result is None:
        return None
    return {
        "target_value": compact_value(sizing_result.target_value),
        "unit_value": compact_value(sizing_result.unit_value),
        "quantity": sizing_result.quantity,
        "estimated_value": compact_value(sizing_result.estimated_value),
    }


def _compact_messages(messages: list[Any] | None) -> list[str] | None:
    if not messages:
        return None
    compacted = []
    for message in messages:
        code = getattr(message, "code", None)
        text = getattr(message, "message", None)
        compacted.append(f"{code}: {text}" if code and text else str(text or code))
    return compacted


def _compact_order_response(response) -> dict[str, Any]:
    result: dict[str, Any] = {}
    order = getattr(response, "order", None)
    if order:
        result["order"] = _compact_order(order)

    buying_power_effect = getattr(response, "buying_power_effect", None)
    if buying_power_effect:
        result["bp_effect"] = compact_row(
            {key: compact_value(value) for key, value in buying_power_effect.model_dump().items()},
            drop_zero_string=True,
            drop_numeric_zero=True,
        )

    fee_calculation = getattr(response, "fee_calculation", None)
    if fee_calculation:
        result["fees"] = compact_row(
            {key: compact_value(value) for key, value in fee_calculation.model_dump().items()},
            drop_zero_string=True,
            drop_numeric_zero=True,
        )

    warnings = _compact_messages(getattr(response, "warnings", None))
    errors = _compact_messages(getattr(response, "errors", None))
    if warnings:
        result["warnings"] = warnings
    if errors:
        result["errors"] = errors
    return result


def _compact_quote_event(event) -> dict[str, Any]:
    data = event.model_dump()
    bid = data.get("bid_price")
    ask = data.get("ask_price")
    if bid is not None and ask is not None:
        mid = (Decimal(str(bid)) + Decimal(str(ask))) / Decimal("2")
        return {
            "sym": compact_value(data.get("event_symbol")),
            "bid": compact_value(bid),
            "ask": compact_value(ask),
            "mid": compact_value(mid),
            "bid_sz": compact_value(data.get("bid_size")),
            "ask_sz": compact_value(data.get("ask_size")),
        }
    return {
        "sym": compact_value(data.get("event_symbol")),
        "last": compact_value(data.get("price")),
        "chg": compact_value(data.get("change")),
        "size": compact_value(data.get("size")),
        "vol": compact_value(data.get("day_volume")),
    }


def _compact_greeks_event(event) -> dict[str, Any]:
    data = event.model_dump()
    return {
        "sym": compact_value(data.get("event_symbol")),
        "price": compact_value(data.get("price")),
        "iv": compact_value(data.get("volatility")),
        "delta": compact_value(data.get("delta")),
        "gamma": compact_value(data.get("gamma")),
        "theta": compact_value(data.get("theta")),
        "vega": compact_value(data.get("vega")),
        "rho": compact_value(data.get("rho")),
    }


def _compact_market_metric(metric) -> dict[str, Any]:
    data = metric.model_dump()
    earnings = getattr(metric, "earnings", None)
    row = {
        "symbol": compact_value(data.get("symbol")),
        "iv_rank": compact_value(data.get("implied_volatility_index_rank")),
        "iv_pct": compact_value(data.get("implied_volatility_percentile")),
        "iv30": compact_value(data.get("implied_volatility_30_day")),
        "hv30": compact_value(data.get("historical_volatility_30_day")),
        "iv_hv30": compact_value(data.get("iv_hv_30_day_difference")),
        "beta": compact_value(data.get("beta")),
        "liq": compact_value(data.get("liquidity_rating")),
        "liq_rank": compact_value(data.get("liquidity_rank")),
        "market_cap": compact_value(data.get("market_cap")),
        "pe": compact_value(data.get("price_earnings_ratio")),
        "eps": compact_value(data.get("earnings_per_share")),
        "div_yield": compact_value(data.get("dividend_yield")),
        "earnings": compact_value(getattr(earnings, "expected_report_date", None)),
    }
    return compact_row(row, drop_zero_string=True)


@mcp_app.tool()
async def account_overview(
    ctx: Context,
    include: list[Literal["balances", "positions"]] | None = None,
) -> str:
    """
    Get balances and/or open positions.

    Args:
        include: Sections to return; defaults to ["balances", "positions"].

    Use get_history(type="transactions", transaction_type="Money Movement") for deposits/withdrawals.
    """
    return tool_xml("account_overview", await build_account_overview(ctx, include))


@mcp_app.tool()
async def get_history(
    ctx: Context,
    type: Literal["transactions", "orders"],
    days: int | None = None,
    underlying_symbol: str | None = None,
    transaction_type: Literal["Trade", "Money Movement"] | None = None,
    page_offset: int = 0,
    limit: int = 25,
) -> str:
    """
    Get paginated transaction or order history.

    Args:
        type: transactions for trade/cash flows, orders for order history.
        days: Lookback; defaults to 90 for transactions, 7 for orders.
        underlying_symbol: Filter by underlying symbol.
        transaction_type: Transactions only: Trade or Money Movement.
        page_offset: Starting offset.
        limit: Page size.
    """
    async with rate_limiter:
        return tool_xml("get_history", await fetch_history(
            ctx,
            type=type,
            days=days,
            underlying_symbol=underlying_symbol,
            transaction_type=transaction_type,
            page_offset=page_offset,
            limit=limit,
        ))


@mcp_app.tool()
async def place_order(
    ctx: Context,
    legs: list[OrderLeg],
    target_value: float | None = None,
    time_in_force: OrderTimeInForce = OrderTimeInForce.DAY,
    dry_run: bool = False,
) -> str:
    """
    Place a new order using live quote-derived mid pricing, rounded to the nearest valid tick.
    No manual limit price is accepted.

    For options, set symbol to the underlying and include option_type, strike_price, expiration_date.
    Quantity is the actual share/contract count. With target_value, omit quantity for single-leg orders;
    for multi-leg spreads, use quantity only to express the leg ratio, such as 1:1 or 2:1.

    Args:
        legs: Equities/options use Buy/Sell to Open/Close; futures use Buy/Sell.
        target_value: Dollar budget; derives whole shares/contracts from current mid pricing.
        time_in_force: Default Day.
        dry_run: Preview without sending.
    """
    if not legs:
        raise ValueError("'legs' is required")

    async with rate_limiter:
        return tool_xml("place_order", await _place_new_order(ctx, legs, time_in_force, target_value, dry_run))


@mcp_app.tool()
async def replace_order(ctx: Context, order_id: str) -> str:
    """
    Reprice a live order once at the current quote-derived mid.
    """
    context = get_context(ctx)
    async with rate_limiter:
        existing_order = await _find_live_order(ctx, order_id)
        resolved_price = await _resolve_replacement_price(ctx, existing_order.legs)
        response = await context.account.replace_order(
            context.session,
            int(order_id),
            build_new_order(
                time_in_force=existing_order.time_in_force,
                legs=existing_order.legs,
                price=resolved_price,
            ),
        )
        return tool_xml("replace_order", _compact_order_response(response))


@mcp_app.tool()
async def cancel_order(ctx: Context, order_id: str) -> str:
    """Cancel a live order by id."""
    context = get_context(ctx)
    await context.account.delete_order(context.session, int(order_id))
    return tool_xml("cancel_order", {"success": True, "order_id": order_id})


@mcp_app.tool()
async def list_orders(ctx: Context) -> str:
    """List all live orders."""
    context = get_context(ctx)
    orders = await context.account.get_live_orders(context.session)
    return tool_xml("list_orders", to_table([_compact_order(order) for order in orders]))


@mcp_app.tool()
async def get_quotes(ctx: Context, instruments: list[InstrumentSpec], timeout: float = 10.0) -> str:
    """
    Get live quotes for stocks, options, futures, and indices.

    Args:
        instruments: Use symbol only for stocks/futures; set instrument_type="Index" for SPX/VIX/NDX; add option fields for options.
        timeout: Seconds to wait for DXLink data.
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
    return tool_xml("get_quotes", to_table([_compact_quote_event(event) for event in events]))


@mcp_app.tool()
async def get_greeks(ctx: Context, options: list[OptionSpec], timeout: float = 10.0) -> str:
    """
    Get option Greeks: delta, gamma, theta, vega, rho.

    Args:
        options: Option contracts by underlying symbol, C/P, strike, and expiration_date.
        timeout: Seconds to wait for DXLink data.
    """
    if not options:
        raise ValueError("At least one option is required")

    session = get_session(ctx)
    option_details = await get_instrument_details(session, [option.to_instrument_spec() for option in options])

    greeks = await _stream_events(session, Greeks, [d.streamer_symbol for d in option_details], timeout)
    return tool_xml("get_greeks", to_table([_compact_greeks_event(greek) for greek in greeks]))


@mcp_app.tool()
async def get_market_metrics(ctx: Context, symbols: list[str]) -> str:
    """
    Get IV/HV, beta, liquidity, valuation, dividends, earnings. IV rank/percentile are 0-1.
    """
    session = get_session(ctx)
    result = await metrics.get_market_metrics(session, symbols)
    return tool_xml("get_market_metrics", to_table([_compact_market_metric(metric) for metric in result]))


@mcp_app.tool()
async def market_status(
    ctx: Context, exchanges: list[Literal["Equity", "CME", "CFE", "Smalls"]] | None = None
) -> str:
    """
    Get exchange open/closed status, next open/close, holiday flags, and current NYC time.
    """
    if exchanges is None:
        exchanges = ["Equity"]
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

    return tool_xml("market_status", {"current_time_nyc": now_in_new_york().isoformat(), "exchanges": results})


@mcp_app.tool()
async def search_symbols(ctx: Context, symbol: str, limit: int = 10) -> str:
    """Search symbols by ticker or company name.

    Args:
        symbol: Query, e.g. AAPL or Apple.
        limit: Max results.
    """
    session = get_session(ctx)
    async with rate_limiter:
        results = await symbol_search(session, symbol)
    return tool_xml("search_symbols", to_table(results[:limit]))


@mcp_app.tool()
async def watchlist(
    ctx: Context,
    action: Literal["list", "add", "remove", "delete"],
    watchlist_type: Literal["public", "private"] = "private",
    name: str | None = None,
    symbols: list[WatchlistSymbol] | None = None,
) -> str:
    """
    Manage watchlists.

    Actions:
        list: no name returns compact watchlists; with name returns symbols. Supports public/private.
        add: add symbols to a private watchlist; creates if missing.
        remove: remove symbols from a private watchlist.
        delete: delete a private watchlist by name.

    Args:
        name: Watchlist name; defaults to main for add/remove/delete.
        symbols: Required for add/remove; each needs symbol and tastytrade instrument_type.
    """
    return tool_xml("watchlist", await manage_watchlist(ctx, action, watchlist_type, name, symbols))


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

Use account_overview, watchlist(action="list") for watchlist names, watchlist(action="list", name=...) for symbols, and get_market_metrics for IV/liquidity data."""),
        AssistantMessage(
            """I'll analyze IV opportunities for your positions and watchlists. Let me start by gathering current positions and watchlist names, then fetch each watchlist's symbols before getting market metrics."""
        ),
    ]
