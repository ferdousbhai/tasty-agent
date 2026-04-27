from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from math import gcd
from typing import Any, Literal

from aiocache import Cache, cached
from aiocache.serializers import PickleSerializer
from pydantic import BaseModel, Field, model_validator
from tastytrade import Session
from tastytrade.instruments import Equity, Future, Option, get_option_chain
from tastytrade.order import InstrumentType, NewOrder, OrderAction, OrderTimeInForce, OrderType

logger = logging.getLogger(__name__)

POSITION_EFFECT_ACTIONS = {
    OrderAction.BUY_TO_OPEN,
    OrderAction.BUY_TO_CLOSE,
    OrderAction.SELL_TO_OPEN,
    OrderAction.SELL_TO_CLOSE,
}
DIRECTIONAL_ACTIONS = {OrderAction.BUY, OrderAction.SELL}
BUY_ACTIONS = {
    OrderAction.BUY,
    OrderAction.BUY_TO_OPEN,
    OrderAction.BUY_TO_CLOSE,
}

CENT = Decimal("0.01")


@dataclass
class InstrumentDetail:
    """Details for a resolved instrument."""

    streamer_symbol: str
    instrument: Equity | Option | Future
    is_index: bool = False
    tick_sizes: list[Any] | None = None


class InstrumentSpec(BaseModel):
    """Specification for an instrument (stock, option, future, or index)."""

    symbol: str = Field(..., description="Symbol (e.g., 'AAPL', '/ESH26', 'SPX')")
    instrument_type: Literal["Equity", "Future", "Index"] | None = Field(
        None,
        description="Instrument type. Auto-detected if omitted: '/' prefix → Future, option fields → Option, otherwise Equity. Use 'Index' for index symbols like SPX, VIX, NDX.",
    )
    option_type: Literal["C", "P"] | None = Field(
        None, description="Option type: 'C' for call, 'P' for put (omit for stocks)"
    )
    strike_price: float | None = Field(None, description="Strike price (required for options)")
    expiration_date: str | None = Field(None, description="Expiration date in YYYY-MM-DD format (required for options)")


class OptionSpec(BaseModel):
    """Specification for an equity option contract."""

    symbol: str = Field(..., description="Underlying symbol (e.g., 'AAPL', 'TSLA')")
    option_type: Literal["C", "P"] = Field(..., description="Option type: 'C' for call, 'P' for put")
    strike_price: float = Field(..., description="Strike price")
    expiration_date: str = Field(..., description="Expiration date in YYYY-MM-DD format")

    def to_instrument_spec(self) -> InstrumentSpec:
        return InstrumentSpec(
            symbol=self.symbol,
            instrument_type=None,
            option_type=self.option_type,
            strike_price=self.strike_price,
            expiration_date=self.expiration_date,
        )


class OrderLeg(BaseModel):
    """Specification for an order leg."""

    symbol: str = Field(..., description="Stock symbol (e.g., 'TQQQ', 'AAPL')")
    action: OrderAction = Field(
        ...,
        description="Use tastytrade order actions. Equities and options use 'Buy to Open', 'Buy to Close', 'Sell to Open', or 'Sell to Close'. Futures use 'Buy' or 'Sell'.",
    )
    quantity: int = Field(
        1,
        ge=1,
        description="Number of contracts/shares. When sizing is supplied, this is the leg ratio and usually stays 1.",
    )
    option_type: Literal["C", "P"] | None = Field(
        None, description="Option type: 'C' for call, 'P' for put (omit for stocks)"
    )
    strike_price: float | None = Field(None, description="Strike price (required for options)")
    expiration_date: str | None = Field(None, description="Expiration date in YYYY-MM-DD format (required for options)")

    @model_validator(mode="after")
    def validate_action_for_instrument(self) -> OrderLeg:
        """Restrict actions to the values accepted for the inferred instrument type."""
        instrument_type = resolve_instrument_type(self.to_instrument_spec())

        if instrument_type == InstrumentType.FUTURE and self.action not in DIRECTIONAL_ACTIONS:
            raise ValueError("Futures must use 'Buy' or 'Sell'.")

        if instrument_type != InstrumentType.FUTURE and self.action not in POSITION_EFFECT_ACTIONS:
            raise ValueError(
                "Equities and options must use one of: Buy to Open, Buy to Close, Sell to Open, Sell to Close."
            )

        return self

    def to_instrument_spec(self) -> InstrumentSpec:
        return InstrumentSpec(
            symbol=self.symbol,
            instrument_type=None,
            option_type=self.option_type,
            strike_price=self.strike_price,
            expiration_date=self.expiration_date,
        )


class PricingPolicy(BaseModel):
    """Intent-based limit pricing for new orders."""

    mode: Literal["mid", "mid_toward_natural"] = Field(
        "mid",
        description=(
            "How the tool should price the order from live quotes. "
            "'mid' uses the current signed net midpoint. "
            "'mid_toward_natural' moves from mid toward the marketable natural price by offset_cents."
        ),
    )
    offset_cents: int = Field(
        0,
        ge=0,
        description="For mode='mid_toward_natural', move this many cents from mid toward the natural price.",
    )
    mid_distance_warning_cents: int | None = Field(
        5,
        ge=0,
        description=(
            "Cent floor for warning when the final limit is far from the current signed net mid-price. "
            "Set null to disable the cent-based warning."
        ),
    )
    mid_distance_warning_spread_fraction: float | None = Field(
        0.25,
        ge=0,
        description=(
            "Spread-relative warning threshold for distance from mid. "
            "The effective warning threshold is max(mid_distance_warning_cents, spread * this fraction)."
        ),
    )

    @model_validator(mode="after")
    def validate_offset_for_mode(self) -> PricingPolicy:
        if self.mode == "mid" and self.offset_cents:
            raise ValueError("offset_cents is only valid when mode='mid_toward_natural'.")
        return self


class OrderSizingPolicy(BaseModel):
    """Dollar-value based sizing for new orders."""

    target_value: Decimal = Field(
        ...,
        gt=0,
        description=(
            "Total dollar premium/notional budget for the order. "
            "The tool derives whole share/contract quantities from live quote-derived pricing."
        ),
    )
    min_quantity: int = Field(1, ge=1, description="Minimum computed order units required.")
    max_quantity: int | None = Field(None, ge=1, description="Optional cap on computed order units.")


@dataclass(frozen=True)
class OrderSizingResult:
    target_value: Decimal
    unit_value: Decimal
    quantity: int
    estimated_value: Decimal


def default_pricing_policy() -> PricingPolicy:
    return PricingPolicy(
        mode="mid",
        offset_cents=0,
        mid_distance_warning_cents=5,
        mid_distance_warning_spread_fraction=0.25,
    )


@dataclass(frozen=True)
class LegQuote:
    """Bid/ask quote resolved for one order leg."""

    symbol: str
    action: OrderAction
    quantity: Decimal
    price_quantity: Decimal
    bid: Decimal
    ask: Decimal
    mid: Decimal


@dataclass(frozen=True)
class OrderMarket:
    """Signed net market for an order.

    Prices are per order unit: 100 shares, 17 single-leg option contracts, or
    17 vertical spreads are quoted as one per-share/per-contract/per-spread price.
    natural_price is the marketable side: buy legs at ask and sell legs at bid.
    passive_price is the optimistic side: buy legs at bid and sell legs at ask.
    The server uses Tastytrade's signed net price convention: debits are negative
    and credits are positive.
    """

    natural_price: Decimal
    passive_price: Decimal
    mid_price: Decimal
    spread: Decimal
    tick_size: Decimal
    legs: tuple[LegQuote, ...]


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


def format_signed_money(value: Decimal) -> str:
    """Format a signed net order price with debit/credit sign preserved."""
    amount = abs(value).quantize(CENT)
    prefix = "-" if value < 0 else ""
    return f"{prefix}${amount:.2f}"


def format_order_market(market: OrderMarket) -> str:
    return (
        f"natural={format_signed_money(market.natural_price)}, "
        f"mid={format_signed_money(market.mid_price)}, "
        f"passive={format_signed_money(market.passive_price)}, "
        f"spread=${market.spread.quantize(CENT):.2f}, "
        f"tick=${market.tick_size.quantize(CENT):.2f}"
    )


def _to_decimal_price(value: Any, label: str) -> Decimal:
    if value is None:
        raise ValueError(f"Missing {label}")
    price = Decimal(str(value))
    if not price.is_finite():
        raise ValueError(f"Invalid {label}: {value}")
    return price


def _round_to_cent(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _round_to_tick(value: Decimal, tick_size: Decimal) -> Decimal:
    if tick_size <= 0:
        raise ValueError(f"Invalid tick size: {tick_size}")
    return (value / tick_size).to_integral_value(rounding=ROUND_HALF_UP) * tick_size


def _validate_quote(detail: InstrumentDetail, bid: Decimal, ask: Decimal) -> None:
    symbol_info = describe_instrument(detail)
    if bid < 0 or ask < 0:
        raise ValueError(f"Invalid quote for {symbol_info}: bid/ask cannot be negative")
    if bid > ask:
        raise ValueError(
            f"Crossed quote for {symbol_info}: bid {format_signed_money(bid)} exceeds ask {format_signed_money(ask)}"
        )


def _has_tick_price_inside(market: OrderMarket) -> bool:
    return market.spread > market.tick_size


def _tick_from_table(tick_sizes: list[Any] | None, price: Decimal) -> Decimal | None:
    if not tick_sizes or not isinstance(tick_sizes, list | tuple):
        return None

    absolute_price = abs(price)
    threshold_matches = [
        tick
        for tick in tick_sizes
        if getattr(tick, "threshold", None) is not None and absolute_price >= Decimal(str(tick.threshold))
    ]
    if threshold_matches:
        tick = max(threshold_matches, key=lambda item: Decimal(str(item.threshold)))
        return Decimal(str(tick.value))

    thresholdless = [tick for tick in tick_sizes if getattr(tick, "threshold", None) is None]
    if thresholdless:
        return Decimal(str(thresholdless[0].value))
    return None


def _instrument_tick_size(detail: InstrumentDetail, price: Decimal) -> Decimal:
    instrument = detail.instrument
    tick_size = _tick_from_table(detail.tick_sizes, price)
    if tick_size:
        return tick_size

    if isinstance(instrument, Future):
        return Decimal(str(instrument.tick_size))

    tick_size = _tick_from_table(getattr(instrument, "tick_sizes", None), price)
    return tick_size or CENT


def order_price_tick_size(
    instrument_details: list[InstrumentDetail],
    leg_quotes: list[LegQuote] | tuple[LegQuote, ...],
) -> Decimal:
    ticks = [
        _instrument_tick_size(detail, leg_quote.mid)
        for detail, leg_quote in zip(instrument_details, leg_quotes, strict=True)
    ]
    return min(ticks) if ticks else CENT


def order_price_tick_cents(instrument_details: list[InstrumentDetail], market: OrderMarket) -> int:
    tick_cents = (order_price_tick_size(instrument_details, market.legs) / CENT).to_integral_value(
        rounding=ROUND_HALF_UP
    )
    return max(1, int(tick_cents))


def _whole_number_quantity(leg: Any) -> int:
    quantity = getattr(leg, "quantity", None)
    if quantity is None:
        raise ValueError("Order leg quantity is required")
    decimal_quantity = Decimal(str(quantity))
    if decimal_quantity <= 0:
        raise ValueError(f"Invalid order leg quantity: {quantity}")
    if decimal_quantity != decimal_quantity.to_integral_value():
        raise ValueError(f"Order leg quantity must be a whole number: {quantity}")
    return int(decimal_quantity)


def _quantity_gcd(legs: list[Any]) -> int:
    unit_size = 0
    for leg in legs:
        quantity = _whole_number_quantity(leg)
        unit_size = quantity if unit_size == 0 else gcd(unit_size, quantity)
    return unit_size or 1


def _price_unit_size(legs: list[Any]) -> Decimal:
    return Decimal(_quantity_gcd(legs))


def build_order_market(
    instrument_details: list[InstrumentDetail],
    legs: list[Any],
    quotes: list[Any],
) -> OrderMarket:
    """Build a signed net bid/ask market from quotes for the exact order instruments."""
    if len(instrument_details) != len(legs) or len(legs) != len(quotes):
        raise ValueError(
            f"Mismatched order inputs: {len(instrument_details)} instruments, {len(legs)} legs, {len(quotes)} quotes"
        )

    natural_price = Decimal("0")
    passive_price = Decimal("0")
    leg_quotes: list[LegQuote] = []
    unit_size = _price_unit_size(legs)

    for detail, leg, quote in zip(instrument_details, legs, quotes, strict=True):
        bid = _to_decimal_price(getattr(quote, "bid_price", None), f"bid price for {describe_instrument(detail)}")
        ask = _to_decimal_price(getattr(quote, "ask_price", None), f"ask price for {describe_instrument(detail)}")
        _validate_quote(detail, bid, ask)

        quantity = Decimal(_whole_number_quantity(leg))
        price_quantity = quantity / unit_size

        mid = (bid + ask) / Decimal("2")
        if leg.action in BUY_ACTIONS:
            natural_price -= ask * price_quantity
            passive_price -= bid * price_quantity
        else:
            natural_price += bid * price_quantity
            passive_price += ask * price_quantity

        leg_quotes.append(
            LegQuote(
                symbol=detail.streamer_symbol,
                action=leg.action,
                quantity=quantity,
                price_quantity=price_quantity,
                bid=bid,
                ask=ask,
                mid=mid,
            )
        )

    mid_price = (natural_price + passive_price) / Decimal("2")
    spread = passive_price - natural_price
    if spread < 0:
        raise ValueError("Invalid order market: natural price exceeds passive price")
    tick_size = order_price_tick_size(instrument_details, leg_quotes)

    return OrderMarket(
        natural_price=natural_price,
        passive_price=passive_price,
        mid_price=mid_price,
        spread=spread,
        tick_size=tick_size,
        legs=tuple(leg_quotes),
    )


def _manual_price_to_decimal(price: float | Decimal, tick_size: Decimal) -> Decimal:
    candidate = _to_decimal_price(price, "manual limit price")
    rounded = _round_to_tick(candidate, tick_size)
    if candidate != rounded:
        raise ValueError(
            f"Manual limit price {format_signed_money(candidate)} is not aligned to the current "
            f"${tick_size.quantize(CENT):.2f} order tick. "
            "Omit manual pricing so the tool computes a valid mid limit from live quotes."
        )
    return _round_to_cent(rounded)


def _policy_price(market: OrderMarket, pricing: PricingPolicy) -> Decimal:
    if pricing.mode == "mid":
        raw_price = market.mid_price
    else:
        offset = Decimal(pricing.offset_cents) / Decimal("100")
        max_offset = max(market.mid_price - market.natural_price, Decimal("0"))
        raw_price = market.mid_price - min(offset, max_offset)

    candidate = _round_to_cent(_round_to_tick(raw_price, market.tick_size))
    if not _has_tick_price_inside(market):
        return min(max(candidate, market.natural_price), market.passive_price)
    if candidate <= market.natural_price:
        return _round_to_cent(_round_to_tick(market.natural_price + market.tick_size, market.tick_size))
    if candidate >= market.passive_price:
        return _round_to_cent(_round_to_tick(market.passive_price - market.tick_size, market.tick_size))
    return candidate


def _validate_limit_price(
    market: OrderMarket,
    candidate: Decimal,
    pricing: PricingPolicy,
) -> list[str]:
    warnings: list[str] = []

    if candidate < market.natural_price or candidate > market.passive_price:
        raise ValueError(
            f"Limit price {format_signed_money(candidate)} is outside the current order market "
            f"({format_order_market(market)}). Omit manual pricing so the tool uses exact-instrument mid pricing."
        )

    if candidate <= market.natural_price or candidate >= market.passive_price:
        if _has_tick_price_inside(market):
            raise ValueError(
                f"Limit price {format_signed_money(candidate)} must be strictly inside the current order market "
                f"({format_order_market(market)})."
            )
        warnings.append(
            "No valid tick price exists strictly inside the current bid/ask spread; "
            f"using boundary price {format_signed_money(candidate)} within {format_order_market(market)}."
        )

    warning_thresholds: list[Decimal] = []
    if pricing.mid_distance_warning_cents is not None:
        warning_thresholds.append(Decimal(pricing.mid_distance_warning_cents) / Decimal("100"))
    if pricing.mid_distance_warning_spread_fraction is not None:
        warning_thresholds.append(market.spread * Decimal(str(pricing.mid_distance_warning_spread_fraction)))
    warning_thresholds.append(market.tick_size)
    if warning_thresholds:
        warning_threshold = max(warning_thresholds)
        distance = abs(candidate - market.mid_price)
        if distance > warning_threshold:
            warnings.append(
                f"Limit price {format_signed_money(candidate)} is ${distance.quantize(CENT):.2f} from mid "
                f"{format_signed_money(market.mid_price)}; warning threshold is ${warning_threshold.quantize(CENT):.2f}. "
                "Verify the user intended this aggressive price."
            )

    return warnings


def resolve_order_price(
    market: OrderMarket,
    pricing: PricingPolicy,
    manual_price: float | Decimal | None = None,
) -> tuple[Decimal, list[str]]:
    """Resolve and validate the final signed limit price for an order."""
    candidate = (
        _manual_price_to_decimal(manual_price, market.tick_size)
        if manual_price is not None
        else _policy_price(market, pricing)
    )
    warnings = _validate_limit_price(market, candidate, pricing)
    return candidate, warnings


def _instrument_order_multiplier(detail: InstrumentDetail) -> Decimal:
    instrument = detail.instrument
    if isinstance(instrument, Option):
        return Decimal(str(instrument.shares_per_contract))
    if isinstance(instrument, Equity):
        return Decimal("1")
    raise ValueError("target_value sizing is currently supported for equities and equity options only")


def _order_dollar_multiplier(instrument_details: list[InstrumentDetail]) -> Decimal:
    multipliers = {_instrument_order_multiplier(detail) for detail in instrument_details}
    if len(multipliers) != 1:
        raise ValueError("target_value sizing requires all legs to share the same dollar multiplier")
    return next(iter(multipliers))


def apply_order_sizing(
    instrument_details: list[InstrumentDetail],
    legs: list[OrderLeg],
    price: Decimal,
    sizing: OrderSizingPolicy | None,
) -> tuple[list[OrderLeg], OrderSizingResult | None]:
    """Scale leg-ratio quantities from a quote-derived per-unit price and dollar budget."""
    if sizing is None:
        return legs, None

    ratio_gcd = _quantity_gcd(legs)
    if ratio_gcd != 1:
        raise ValueError(
            "When sizing is supplied, leg quantities must be the smallest whole-number ratio "
            "(for a single option/stock use quantity=1)."
        )

    unit_value = abs(price) * _order_dollar_multiplier(instrument_details)
    if unit_value <= 0:
        raise ValueError("Cannot size an order with a zero dollar unit value")

    computed_quantity = int((sizing.target_value / unit_value).to_integral_value(rounding=ROUND_FLOOR))
    if sizing.max_quantity is not None:
        computed_quantity = min(computed_quantity, sizing.max_quantity)
    if computed_quantity < sizing.min_quantity:
        raise ValueError(
            f"target_value ${sizing.target_value} is too small for one order unit at ${unit_value.quantize(CENT)}."
        )

    sized_legs = [leg.model_copy(update={"quantity": leg.quantity * computed_quantity}) for leg in legs]
    result = OrderSizingResult(
        target_value=sizing.target_value,
        unit_value=unit_value,
        quantity=computed_quantity,
        estimated_value=unit_value * computed_quantity,
    )
    return sized_legs, result


def _option_chain_key_builder(fn, session: Session, symbol: str):
    """Build cache key using only symbol (session changes but symbol is stable)."""
    return f"option_chain:{symbol}"


@cached(ttl=86400, cache=Cache.MEMORY, serializer=PickleSerializer(), key_builder=_option_chain_key_builder)
async def get_cached_option_chain(session: Session, symbol: str):
    """Cache option chains for 24 hours as they rarely change during that timeframe."""
    return await get_option_chain(session, symbol)


def resolve_instrument_type(spec: InstrumentSpec) -> InstrumentType:
    """Determine instrument type from spec fields."""
    if spec.instrument_type:
        return InstrumentType(spec.instrument_type)
    if spec.option_type:
        return InstrumentType.EQUITY_OPTION
    if spec.symbol.startswith("/"):
        return InstrumentType.FUTURE
    return InstrumentType.EQUITY


async def _lookup_option_detail(session: Session, spec: InstrumentSpec, symbol: str) -> InstrumentDetail:
    """Resolve an equity option contract from the cached option chain."""
    if not spec.option_type:
        raise ValueError(f"option_type ('C' or 'P') is required for option {symbol}")
    option_type = spec.option_type
    strike_price = validate_strike_price(spec.strike_price)
    expiration_date = spec.expiration_date
    if not expiration_date:
        raise ValueError(f"expiration_date is required for option {symbol}")

    target_date = validate_date_format(expiration_date)
    chain = await get_cached_option_chain(session, symbol)
    if target_date not in chain:
        available_dates = sorted(chain.keys())
        raise ValueError(f"No options found for {symbol} expiration {expiration_date}. Available: {available_dates}")

    for option in chain[target_date]:
        if option.strike_price == strike_price and option.option_type.value == option_type:
            underlying = await Equity.get(session, symbol)
            return InstrumentDetail(
                option.streamer_symbol,
                option,
                tick_sizes=underlying.option_tick_sizes,
            )

    available_strikes = [opt.strike_price for opt in chain[target_date] if opt.option_type.value == option_type]
    raise ValueError(
        f"Option not found: {symbol} {expiration_date} {option_type} {strike_price}. Available strikes: {sorted(set(available_strikes))}"
    )


async def _lookup_future_detail(session: Session, symbol: str) -> InstrumentDetail:
    """Resolve a future contract."""
    instrument = await Future.get(session, symbol)
    return InstrumentDetail(instrument.streamer_symbol, instrument)


async def _lookup_equity_detail(session: Session, symbol: str, is_index: bool = False) -> InstrumentDetail:
    """Resolve an equity or index instrument."""
    instrument = await Equity.get(session, symbol)
    streamer_symbol = instrument.streamer_symbol if is_index else symbol
    return InstrumentDetail(streamer_symbol, instrument, is_index=is_index)


async def _lookup_order_leg_detail(session: Session, leg: Any) -> InstrumentDetail:
    """Resolve an existing broker order leg to its streamable instrument."""
    symbol = getattr(leg, "symbol", None)
    instrument_type = getattr(leg, "instrument_type", None)
    if not symbol or not instrument_type:
        raise ValueError("Existing order leg is missing symbol or instrument_type")

    resolved_type = InstrumentType(instrument_type)
    if resolved_type == InstrumentType.EQUITY_OPTION:
        instrument = await Option.get(session, symbol)
        underlying = await Equity.get(session, instrument.underlying_symbol)
        return InstrumentDetail(
            instrument.streamer_symbol,
            instrument,
            tick_sizes=underlying.option_tick_sizes,
        )
    if resolved_type == InstrumentType.FUTURE:
        instrument = await Future.get(session, symbol)
        return InstrumentDetail(instrument.streamer_symbol, instrument)
    if resolved_type == InstrumentType.EQUITY:
        instrument = await Equity.get(session, symbol)
        return InstrumentDetail(instrument.streamer_symbol or symbol, instrument)

    raise ValueError(f"Replacement pricing is not supported for {resolved_type.value} legs")


async def get_order_leg_instrument_details(session: Session, legs: list[Any]) -> list[InstrumentDetail]:
    """Resolve existing broker order legs for quote-based replacement pricing."""
    return await asyncio.gather(*[_lookup_order_leg_detail(session, leg) for leg in legs])


async def get_instrument_details(session: Session, instrument_specs: list[InstrumentSpec]) -> list[InstrumentDetail]:
    """Get instrument details with validation and caching."""

    async def lookup_single_instrument(spec: InstrumentSpec) -> InstrumentDetail:
        symbol = spec.symbol.upper()
        resolved_type = resolve_instrument_type(spec)

        if resolved_type == InstrumentType.EQUITY_OPTION:
            return await _lookup_option_detail(session, spec, symbol)

        if resolved_type == InstrumentType.FUTURE:
            return await _lookup_future_detail(session, symbol)

        if resolved_type == InstrumentType.INDEX:
            return await _lookup_equity_detail(session, symbol, is_index=True)

        return await _lookup_equity_detail(session, symbol)

    return await asyncio.gather(*[lookup_single_instrument(spec) for spec in instrument_specs])


def build_order_legs(instrument_details: list[InstrumentDetail], legs: list[OrderLeg]) -> list:
    """Build order legs from instrument details and leg specifications."""
    if len(instrument_details) != len(legs):
        raise ValueError(f"Mismatched legs: {len(instrument_details)} instruments vs {len(legs)} leg specs")

    built_legs = []
    for detail, leg_spec in zip(instrument_details, legs, strict=True):
        instrument = detail.instrument
        if isinstance(instrument, Equity) and instrument.is_index:
            raise ValueError(f"Cannot place orders for index symbol '{detail.streamer_symbol}' (quote-only)")
        built_legs.append(instrument.build_leg(Decimal(str(leg_spec.quantity)), leg_spec.action))
    return built_legs


def describe_instrument(detail: InstrumentDetail) -> str:
    """Build a concise instrument label for errors and logs."""
    instrument = detail.instrument
    if isinstance(instrument, Option):
        return (
            f"{instrument.underlying_symbol} "
            f"{instrument.option_type.value}{instrument.strike_price} {instrument.expiration_date}"
        )
    return instrument.symbol


def build_new_order(
    time_in_force: OrderTimeInForce,
    legs: list,
    price: Decimal | float,
) -> NewOrder:
    """Build a limit order from resolved legs and price."""
    return NewOrder(
        time_in_force=time_in_force,
        order_type=OrderType.LIMIT,
        legs=legs,
        price=Decimal(str(price)),
    )


async def find_live_order(account, session: Session, order_id: str):
    """Return a live order by id, raising a helpful error if missing."""
    live_orders = await account.get_live_orders(session)
    existing_order = next((order for order in live_orders if str(order.id) == order_id), None)

    if existing_order is None:
        live_order_ids = [str(order.id) for order in live_orders]
        logger.warning(f"Order {order_id} not found in live orders. Available orders: {live_order_ids}")
        raise ValueError(f"Order {order_id} not found in live orders")

    return existing_order
