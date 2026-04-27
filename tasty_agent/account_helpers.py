from __future__ import annotations

import asyncio
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Literal

from mcp.server.fastmcp import Context

from tasty_agent.core import compact_row, compact_value, get_context, to_table

BALANCE_FIELDS = {
    "net_liquidating_value": "net_liq",
    "cash_balance": "cash",
    "cash_available_to_withdraw": "cash_withdraw",
    "equity_buying_power": "bp_equity",
    "derivative_buying_power": "bp_deriv",
    "day_trading_buying_power": "bp_day",
    "available_trading_funds": "avail_funds",
    "maintenance_requirement": "maint_req",
    "maintenance_excess": "maint_excess",
    "futures_margin_requirement": "fut_margin",
    "used_derivative_buying_power": "used_deriv_bp",
    "updated_at": "updated_at",
}


POSITION_FIELDS = {
    "symbol": "symbol",
    "instrument_type": "type",
    "underlying_symbol": "underlying",
    "quantity": "qty",
    "quantity_direction": "dir",
    "average_open_price": "avg_open",
    "mark_price": "mark",
    "realized_day_gain": "day_gain",
    "realized_today": "today",
    "expires_at": "expires",
}


def _compact_balances(balance) -> dict[str, Any]:
    data = balance.model_dump()
    return compact_row(
        {output_key: compact_value(data.get(source_key)) for source_key, output_key in BALANCE_FIELDS.items()},
        drop_zero_string=True,
    )


def _compact_positions(positions: list[Any]) -> list[dict[str, Any]]:
    rows = []
    for position in positions:
        data = position.model_dump()
        row = {output_key: compact_value(data.get(source_key)) for source_key, output_key in POSITION_FIELDS.items()}
        rows.append(compact_row(row, drop_zero_string=True))
    return rows


def _compact_text(value: Any, max_chars: int = 80) -> str | None:
    text = compact_value(value)
    if not text:
        return None
    text = str(text)
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3]}..."


def _money_sum(*values: Any) -> Decimal:
    total = Decimal("0")
    for value in values:
        if value is not None:
            total += Decimal(str(value))
    return total


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


def _compact_transaction(transaction) -> dict[str, Any]:
    data = transaction.model_dump()
    fees = _money_sum(
        data.get("regulatory_fees"),
        data.get("clearing_fees"),
        data.get("commission"),
        data.get("proprietary_index_option_fees"),
        data.get("other_charge"),
    )
    row = {
        "date": compact_value(data.get("executed_at") or data.get("transaction_date")),
        "type": compact_value(data.get("transaction_type")),
        "sub_type": compact_value(data.get("transaction_sub_type")),
        "symbol": compact_value(data.get("symbol")),
        "action": compact_value(data.get("action")),
        "qty": compact_value(data.get("quantity")),
        "price": compact_value(data.get("price")),
        "value": compact_value(data.get("value")),
        "net": compact_value(data.get("net_value")),
        "fees": compact_value(fees) if fees else None,
        "order_id": compact_value(data.get("order_id")),
        "desc": _compact_text(data.get("description")),
    }
    return compact_row(row, drop_zero_string=True)


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


async def build_account_overview(
    ctx: Context,
    include: list[Literal["balances", "positions"]] | None = None,
) -> dict[str, Any]:
    """Fetch account balances and/or positions and format the response."""
    if include is None:
        include = ["balances", "positions"]

    context = get_context(ctx)
    session = context.session
    result: dict[str, Any] = {}

    tasks: dict[str, Any] = {}
    if "balances" in include:
        tasks["balances"] = context.account.get_balances(session)
    if "positions" in include:
        tasks["positions"] = context.account.get_positions(session, include_marks=True)

    fetched = await asyncio.gather(*tasks.values())
    for key, value in zip(tasks.keys(), fetched, strict=True):
        if key == "balances":
            result["balances"] = _compact_balances(value)
        elif key == "positions":
            result["positions"] = _compact_positions(value)

    return result


async def fetch_history(
    ctx: Context,
    type: Literal["transactions", "orders"],
    days: int | None = None,
    underlying_symbol: str | None = None,
    transaction_type: Literal["Trade", "Money Movement"] | None = None,
    page_offset: int = 0,
    limit: int = 25,
) -> str:
    """Fetch transaction or order history and return it as a table."""
    context = get_context(ctx)
    session = context.session
    effective_days = days if days is not None else (90 if type == "transactions" else 7)
    start = date.today() - timedelta(days=effective_days)

    if type == "transactions":
        items = await context.account.get_history(
            session,
            start_date=start,
            underlying_symbol=underlying_symbol,
            type=transaction_type,
            per_page=limit,
            page_offset=page_offset,
        )
    else:
        items = await context.account.get_order_history(
            session,
            start_date=start,
            underlying_symbol=underlying_symbol,
            per_page=limit,
            page_offset=page_offset,
        )

    if type == "transactions":
        return to_table([_compact_transaction(item) for item in items or []])
    return to_table([_compact_order(item) for item in items or []])
