from __future__ import annotations

import logging
from typing import Any, Literal

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field
from tastytrade.order import InstrumentType
from tastytrade.watchlists import PrivateWatchlist, PublicWatchlist

from tasty_agent.core import compact_row, get_session

logger = logging.getLogger(__name__)


class WatchlistSymbol(BaseModel):
    """Symbol specification for watchlist operations."""

    symbol: str = Field(..., description="Symbol, e.g. AAPL.")
    instrument_type: Literal["Equity", "Equity Option", "Future", "Future Option", "Cryptocurrency", "Warrant"] = Field(
        ..., description="Tastytrade instrument type."
    )


def _symbol_list(symbols: list[WatchlistSymbol]) -> str:
    """Format symbols for status messages."""
    return ", ".join(f"{s.symbol} ({s.instrument_type})" for s in symbols)


def _watchlist_entries(symbols: list[WatchlistSymbol]) -> list[dict[str, str]]:
    """Convert watchlist symbols to tastytrade upload entries."""
    return [{"symbol": s.symbol, "instrument_type": s.instrument_type} for s in symbols]


def _compact_watchlist(watchlist, *, include_symbols: bool) -> dict[str, Any]:
    """Return watchlist metadata with compact symbol entries."""
    data = watchlist.model_dump()
    entries = data.get("watchlist_entries") or []
    symbols = []
    for entry in entries:
        symbol = entry.get("symbol")
        instrument_type = entry.get("instrument_type")
        symbols.append(f"{symbol}:{instrument_type}" if instrument_type else symbol)
    result: dict[str, Any] = {
        "name": data.get("name"),
        "group": data.get("group_name"),
        "symbol_count": len(symbols),
    }
    if include_symbols:
        result["symbols"] = symbols
    return compact_row(result)


async def manage_watchlist(
    ctx: Context,
    action: Literal["list", "add", "remove", "delete"],
    watchlist_type: Literal["public", "private"] = "private",
    name: str | None = None,
    symbols: list[WatchlistSymbol] | None = None,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Manage watchlists: list, add symbols, remove symbols, or delete."""
    session = get_session(ctx)

    if action == "list":
        watchlist_class = PublicWatchlist if watchlist_type == "public" else PrivateWatchlist
        if name:
            return _compact_watchlist(await watchlist_class.get(session, name), include_symbols=True)
        return [
            _compact_watchlist(watchlist, include_symbols=False) for watchlist in await watchlist_class.get(session)
        ]

    effective_name = name or "main"

    if action == "delete":
        await PrivateWatchlist.remove(session, effective_name)
        return {"status": "deleted", "name": effective_name}

    if not symbols:
        raise ValueError(f"'symbols' is required for action='{action}'")

    if action == "add":
        return await _add_watchlist_symbols(ctx, session, effective_name, symbols)

    return await _remove_watchlist_symbols(ctx, session, effective_name, symbols)


async def _add_watchlist_symbols(
    ctx: Context,
    session,
    watchlist_name: str,
    symbols: list[WatchlistSymbol],
) -> dict[str, Any]:
    """Add symbols to a private watchlist, creating it if needed."""
    symbol_list = _symbol_list(symbols)
    try:
        watchlist = await PrivateWatchlist.get(session, watchlist_name)
    except Exception:
        watchlist = PrivateWatchlist(
            name=watchlist_name,
            group_name="main",
            watchlist_entries=_watchlist_entries(symbols),
        )
        await watchlist.upload(session)
        logger.info("Created new watchlist '%s' with %s symbols", watchlist_name, len(symbols))
        await ctx.info(f"Created watchlist '{watchlist_name}' and added {len(symbols)} symbols: {symbol_list}")
        return {"status": "created", "name": watchlist_name, "symbols_added": len(symbols)}

    for symbol_spec in symbols:
        watchlist.add_symbol(symbol_spec.symbol, InstrumentType(symbol_spec.instrument_type))
    await watchlist.update(session)
    logger.info("Added %s symbols to existing watchlist '%s'", len(symbols), watchlist_name)
    await ctx.info(f"Added {len(symbols)} symbols to watchlist '{watchlist_name}': {symbol_list}")
    return {"status": "added", "name": watchlist_name, "symbols_added": len(symbols)}


async def _remove_watchlist_symbols(
    ctx: Context,
    session,
    watchlist_name: str,
    symbols: list[WatchlistSymbol],
) -> dict[str, Any]:
    """Remove symbols from a private watchlist."""
    watchlist = await PrivateWatchlist.get(session, watchlist_name)
    for symbol_spec in symbols:
        watchlist.remove_symbol(symbol_spec.symbol, InstrumentType(symbol_spec.instrument_type))
    await watchlist.update(session)
    symbol_list = _symbol_list(symbols)
    logger.info("Removed %s symbols from watchlist '%s'", len(symbols), watchlist_name)
    await ctx.info(f"Removed {len(symbols)} symbols from watchlist '{watchlist_name}': {symbol_list}")
    return {"status": "removed", "name": watchlist_name, "symbols_removed": len(symbols)}
