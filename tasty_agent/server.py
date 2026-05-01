import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

import humanize
from aiolimiter import AsyncLimiter
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.prompts.base import AssistantMessage, Message, UserMessage
from tastytrade import metrics
from tastytrade.dxfeed import Greeks, Quote, Summary
from tastytrade.instruments import Option
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
    stream_multi_events as _stream_multi_events,
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
    PricingPolicy,
    apply_order_sizing,
    build_new_order,
    build_order_legs,
    build_order_market,
    default_pricing_policy,
    find_live_order,
    format_order_market,
    format_signed_money,
    get_cached_option_chain,
    get_instrument_details,
    get_order_leg_instrument_details,
    order_price_tick_cents,
    resolve_order_price,
    validate_date_format,
)
from tasty_agent.watchlists import WatchlistSymbol, manage_watchlist

logger = logging.getLogger(__name__)

rate_limiter = AsyncLimiter(2, 1)  # 2 requests per second
CHASE_INTERVAL_SECONDS = 10.0
CHASE_MAX_ATTEMPTS = 10
CHASE_STEP_TICKS = 1

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


async def _fetch_order_market(ctx: Context, instrument_details: list[InstrumentDetail], legs: list[Any]):
    """Fetch current quotes and build the signed net market for order legs."""
    session = get_session(ctx)
    quotes = await _stream_events(session, Quote, [d.streamer_symbol for d in instrument_details], timeout=10.0)
    return build_order_market(instrument_details, legs, quotes)


def _pricing_policy_from_offset(offset_cents: int | None) -> PricingPolicy:
    """Build the internal pricing policy used by mid pricing and chase reprices."""
    if offset_cents is None or offset_cents == 0:
        return default_pricing_policy()
    if offset_cents < 0:
        raise ValueError("Price offset must be greater than or equal to 0")
    return PricingPolicy(
        mode="mid_toward_natural",
        offset_cents=offset_cents,
        mid_distance_warning_cents=5,
        mid_distance_warning_spread_fraction=0.25,
    )


def _pricing_label(pricing_policy: PricingPolicy) -> str:
    if pricing_policy.mode == "mid":
        return "mid"
    return f"mid {pricing_policy.offset_cents}c toward natural"


async def calculate_net_price(ctx: Context, instrument_details: list[InstrumentDetail], legs: list[OrderLeg]) -> float:
    """Calculate a guarded signed net mid-price from current market quotes."""
    market = await _fetch_order_market(ctx, instrument_details, legs)
    resolved_price, _ = resolve_order_price(market, default_pricing_policy())
    return float(resolved_price)


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
            f"from {_pricing_label(pricing_policy)} ({format_order_market(market)})."
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
    chase: bool,
) -> dict[str, Any]:
    """Resolve a new order and place it."""
    if chase and target_value is not None:
        await ctx.warning(
            "Chase repricing keeps the initially sized quantity fixed, so final notional/premium "
            "can move above target_value if the market moves or the order is repriced toward natural."
        )

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
    if chase:
        if dry_run:
            result["chase"] = {"status": "skipped_dry_run"}
        else:
            result["chase"] = await _chase_live_order(ctx, response)
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
    offset_cents: int | None = None,
) -> Decimal:
    pricing_policy = _pricing_policy_from_offset(offset_cents)
    resolved_price, warnings = resolve_order_price(market, pricing_policy)
    for warning in warnings:
        await ctx.warning(warning)
    await ctx.info(
        f"Resolved replacement limit {format_signed_money(resolved_price)} "
        f"from {_pricing_label(pricing_policy)} ({format_order_market(market)})."
    )
    return resolved_price


def _response_order_id(response) -> str | None:
    order = getattr(response, "order", None)
    order_id = getattr(order, "id", None)
    return str(order_id) if order_id is not None else None


async def _get_live_order(ctx: Context, order_id: str):
    context = get_context(ctx)
    live_orders = await context.account.get_live_orders(context.session)
    return next((order for order in live_orders if str(order.id) == str(order_id)), None)


async def _chase_live_order(
    ctx: Context,
    initial_response,
) -> dict[str, Any]:
    """Check a live order and reprice toward natural using fresh quotes each attempt."""
    current_order_id = _response_order_id(initial_response)
    if current_order_id is None:
        return {"status": "no_order_id"}

    context = get_context(ctx)
    last_price: Decimal | None = None
    last_step_ticks = 0
    last_tick_cents = 0
    reprices = 0

    for attempt in range(1, CHASE_MAX_ATTEMPTS + 1):
        await asyncio.sleep(CHASE_INTERVAL_SECONDS)
        live_order = await _get_live_order(ctx, current_order_id)
        if live_order is None:
            return {
                "status": "not_live",
                "checks": attempt,
                "reprices": reprices,
                "order_id": current_order_id,
            }

        session = get_session(ctx)
        instrument_details = await get_order_leg_instrument_details(session, live_order.legs)
        market = await _fetch_order_market(ctx, instrument_details, live_order.legs)
        last_tick_cents = order_price_tick_cents(instrument_details, market)
        last_step_ticks = attempt * CHASE_STEP_TICKS
        offset_cents = last_step_ticks * last_tick_cents
        last_price = await _resolve_replacement_market_price(ctx, market, offset_cents=offset_cents)
        response = await context.account.replace_order(
            context.session,
            int(current_order_id),
            build_new_order(
                time_in_force=live_order.time_in_force,
                legs=live_order.legs,
                price=last_price,
            ),
        )
        reprices += 1
        current_order_id = _response_order_id(response) or current_order_id

    return {
        "status": "still_live",
        "checks": CHASE_MAX_ATTEMPTS,
        "reprices": reprices,
        "order_id": current_order_id,
        "last_step_ticks": last_step_ticks,
        "last_tick_cents": last_tick_cents,
        "last_price": compact_value(last_price),
    }


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
) -> dict[str, Any]:
    """
    Get balances and/or open positions.

    Args:
        include: Sections to return; defaults to ["balances", "positions"].

    Use get_history(type="transactions", transaction_type="Money Movement") for deposits/withdrawals.
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
        return await fetch_history(
            ctx,
            type=type,
            days=days,
            underlying_symbol=underlying_symbol,
            transaction_type=transaction_type,
            page_offset=page_offset,
            limit=limit,
        )


@mcp_app.tool()
async def place_order(
    ctx: Context,
    legs: list[OrderLeg],
    target_value: float | None = None,
    chase: bool = True,
    time_in_force: OrderTimeInForce = OrderTimeInForce.DAY,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Place a new order using live quote-derived mid pricing. No manual limit price is accepted.

    For options, set symbol to the underlying and include option_type, strike_price, expiration_date.
    Multi-leg quantities are ratios when target_value is set.

    Args:
        legs: Equities/options use Buy/Sell to Open/Close; futures use Buy/Sell.
        target_value: Dollar budget; derives whole shares/contracts from current mid pricing.
        chase: If true, reprice live orders every 10s toward fill, up to 10 times.
        time_in_force: Default Day.
        dry_run: Preview without sending.
    """
    if not legs:
        raise ValueError("'legs' is required")

    async with rate_limiter:
        return await _place_new_order(ctx, legs, time_in_force, target_value, dry_run, chase)


@mcp_app.tool()
async def replace_order(ctx: Context, order_id: str) -> dict[str, Any]:
    """
    Reprice a live order once at the current quote-derived mid. For automatic repricing, use place_order(chase=true).
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
        return _compact_order_response(response)


@mcp_app.tool()
async def cancel_order(ctx: Context, order_id: str) -> dict[str, Any]:
    """Cancel a live order by id."""
    context = get_context(ctx)
    await context.account.delete_order(context.session, int(order_id))
    return {"success": True, "order_id": order_id}


@mcp_app.tool()
async def list_orders(ctx: Context) -> str:
    """List all live orders."""
    context = get_context(ctx)
    orders = await context.account.get_live_orders(context.session)
    return to_table([_compact_order(order) for order in orders])


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
    return to_table([_compact_quote_event(event) for event in events])


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
    return to_table([_compact_greeks_event(greek) for greek in greeks])


@mcp_app.tool()
async def get_gex(
    ctx: Context,
    symbol: str,
    expiration_date: str,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """
    Get GEX for one option expiration: net GEX, regime, flip level, call/put walls, top strikes.

    Args:
        symbol: Underlying symbol, e.g. SPY, SPX, AAPL.
        expiration_date: YYYY-MM-DD.
        timeout: Seconds to wait; large chains may need more.
    """
    session = get_session(ctx)
    target_date = validate_date_format(expiration_date)

    chain = await get_cached_option_chain(session, symbol.upper())
    if target_date not in chain:
        available_dates = sorted(chain.keys())
        raise ValueError(f"No options for {symbol.upper()} expiration {expiration_date}. Available: {available_dates}")

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
    call_wall = max(positive_strikes, key=lambda strike: positive_strikes[strike]) if positive_strikes else None

    # Put wall: strike with most negative GEX
    negative_strikes = {s: g for s, g in strike_gex.items() if g < 0}
    put_wall = min(negative_strikes, key=lambda strike: negative_strikes[strike]) if negative_strikes else None

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
    Get IV/HV, beta, liquidity, valuation, dividends, earnings. IV rank/percentile are 0-1.
    """
    session = get_session(ctx)
    result = await metrics.get_market_metrics(session, symbols)
    return to_table([_compact_market_metric(metric) for metric in result])


@mcp_app.tool()
async def market_status(
    ctx: Context, exchanges: list[Literal["Equity", "CME", "CFE", "Smalls"]] | None = None
) -> dict[str, Any]:
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

    return {"current_time_nyc": now_in_new_york().isoformat(), "exchanges": results}


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
    return to_table(results[:limit])


@mcp_app.tool()
async def watchlist(
    ctx: Context,
    action: Literal["list", "add", "remove", "delete"],
    watchlist_type: Literal["public", "private"] = "private",
    name: str | None = None,
    symbols: list[WatchlistSymbol] | None = None,
) -> list[dict[str, Any]] | dict[str, Any]:
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

Use account_overview, watchlist(action="list") for watchlist names, watchlist(action="list", name=...) for symbols, and get_market_metrics for IV/liquidity data."""),
        AssistantMessage(
            """I'll analyze IV opportunities for your positions and watchlists. Let me start by gathering current positions and watchlist names, then fetch each watchlist's symbols before getting market metrics."""
        ),
    ]
