from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Literal, Any

from mcp.server.fastmcp import Context

from tasty_agent.core import get_context, to_table


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
    for key, value in zip(tasks.keys(), fetched):
        if key == "balances":
            result["balances"] = {k: v for k, v in value.model_dump().items() if v is not None and v != 0}
        elif key == "positions":
            result["positions"] = to_table(value)

    return result


async def fetch_history(
    ctx: Context,
    type: Literal["transactions", "orders"],
    days: int | None = None,
    underlying_symbol: str | None = None,
    transaction_type: Literal["Trade", "Money Movement"] | None = None,
    page_offset: int = 0,
    max_results: int = 50,
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
            per_page=max_results,
            page_offset=page_offset,
        )
    else:
        items = await context.account.get_order_history(
            session,
            start_date=start,
            underlying_symbol=underlying_symbol,
            per_page=max_results,
            page_offset=page_offset,
        )

    return to_table(items or [])
