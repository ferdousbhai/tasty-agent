from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
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


@dataclass
class InstrumentDetail:
    """Details for a resolved instrument."""
    streamer_symbol: str
    instrument: Equity | Option | Future
    is_index: bool = False


class InstrumentSpec(BaseModel):
    """Specification for an instrument (stock, option, future, or index)."""
    symbol: str = Field(..., description="Symbol (e.g., 'AAPL', '/ESH26', 'SPX')")
    instrument_type: Literal['Equity', 'Future', 'Index'] | None = Field(None, description="Instrument type. Auto-detected if omitted: '/' prefix → Future, option fields → Option, otherwise Equity. Use 'Index' for index symbols like SPX, VIX, NDX.")
    option_type: Literal['C', 'P'] | None = Field(None, description="Option type: 'C' for call, 'P' for put (omit for stocks)")
    strike_price: float | None = Field(None, description="Strike price (required for options)")
    expiration_date: str | None = Field(None, description="Expiration date in YYYY-MM-DD format (required for options)")


class OrderLeg(BaseModel):
    """Specification for an order leg."""
    symbol: str = Field(..., description="Stock symbol (e.g., 'TQQQ', 'AAPL')")
    action: OrderAction = Field(..., description="Use tastytrade order actions. Equities and options use 'Buy to Open', 'Buy to Close', 'Sell to Open', or 'Sell to Close'. Futures use 'Buy' or 'Sell'.")
    quantity: int = Field(..., description="Number of contracts/shares")
    option_type: Literal['C', 'P'] | None = Field(None, description="Option type: 'C' for call, 'P' for put (omit for stocks)")
    strike_price: float | None = Field(None, description="Strike price (required for options)")
    expiration_date: str | None = Field(None, description="Expiration date in YYYY-MM-DD format (required for options)")

    @model_validator(mode="after")
    def validate_action_for_instrument(self) -> "OrderLeg":
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
            option_type=self.option_type,
            strike_price=self.strike_price,
            expiration_date=self.expiration_date,
        )


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
    return await get_option_chain(session, symbol)


def resolve_instrument_type(spec: InstrumentSpec) -> InstrumentType:
    """Determine instrument type from spec fields."""
    if spec.instrument_type:
        return InstrumentType(spec.instrument_type)
    if spec.option_type:
        return InstrumentType.EQUITY_OPTION
    if spec.symbol.startswith('/'):
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
            return InstrumentDetail(option.streamer_symbol, option)

    available_strikes = [opt.strike_price for opt in chain[target_date] if opt.option_type.value == option_type]
    raise ValueError(f"Option not found: {symbol} {expiration_date} {option_type} {strike_price}. Available strikes: {sorted(set(available_strikes))}")


async def _lookup_future_detail(session: Session, symbol: str) -> InstrumentDetail:
    """Resolve a future contract."""
    instrument = await Future.get(session, symbol)
    return InstrumentDetail(instrument.streamer_symbol, instrument)


async def _lookup_equity_detail(session: Session, symbol: str, is_index: bool = False) -> InstrumentDetail:
    """Resolve an equity or index instrument."""
    instrument = await Equity.get(session, symbol)
    streamer_symbol = instrument.streamer_symbol if is_index else symbol
    return InstrumentDetail(streamer_symbol, instrument, is_index=is_index)


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
    price: float,
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
