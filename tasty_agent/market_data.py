from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import humanize
from tastytrade import Session
from tastytrade.dxfeed import Greeks, Quote, Summary, Trade
from tastytrade.market_sessions import ExchangeType, MarketStatus, get_market_sessions
from tastytrade.streamer import DXLinkStreamer

logger = logging.getLogger(__name__)


def exchanges_for_symbols(streamer_symbols: list[str]) -> set[ExchangeType]:
    """Determine which exchanges to check based on streamer symbols."""
    exchanges: set[ExchangeType] = set()
    for sym in streamer_symbols:
        if sym.startswith("/"):
            if ":XCBF" in sym or sym.startswith("/VX"):
                exchanges.add(ExchangeType.CFE)
            else:
                exchanges.add(ExchangeType.CME)
        else:
            exchanges.add(ExchangeType.NYSE)
    return exchanges


def get_next_open_time(session, current_time: datetime) -> datetime | None:
    """Determine next market open time based on current status."""
    if session.status == MarketStatus.PRE_MARKET:
        return session.open_at
    if session.status == MarketStatus.CLOSED:
        if session.open_at and current_time < session.open_at:
            return session.open_at
        if session.close_at and current_time > session.close_at and session.next_session:
            return session.next_session.open_at
    if session.status == MarketStatus.EXTENDED and session.next_session:
        return session.next_session.open_at
    return None


async def market_status_message(session: Session, exchanges: set[ExchangeType]) -> str | None:
    """Check if relevant markets are closed and return a message, or None if open."""
    try:
        market_sessions = await get_market_sessions(session, list(exchanges))
    except Exception:
        logger.debug("Failed to fetch market sessions for %s", exchanges, exc_info=True)
        return None

    current_time = datetime.now(UTC)
    closed: list[str] = []
    for ms in market_sessions:
        if ms.status != MarketStatus.OPEN:
            next_open = get_next_open_time(ms, current_time)
            label = ms.instrument_collection
            if next_open:
                delta = humanize.naturaldelta(next_open - current_time)
                closed.append(f"{label} (opens in {delta})")
            else:
                closed.append(f"{label} (closed)")
    if closed:
        return f"Market is currently closed: {', '.join(closed)}. Live quotes are not available while the market is closed."
    return None


async def raise_with_market_context(
    session: Session,
    exchanges: set[ExchangeType],
    fallback_error: ValueError,
) -> None:
    """Raise a market-closed message if applicable, otherwise raise the fallback error."""
    market_msg = await market_status_message(session, exchanges)
    if market_msg:
        raise ValueError(market_msg) from fallback_error
    raise fallback_error


async def stream_events(
    session: Session,
    event_type: type[Quote] | type[Greeks],
    streamer_symbols: list[str],
    timeout: float,
) -> list[Any]:
    """Generic streaming helper for Quote/Greeks events."""
    events_by_symbol: dict[str, Any] = {}
    expected = set(streamer_symbols)
    exchanges = exchanges_for_symbols(streamer_symbols)
    timed_out = False
    try:
        async with DXLinkStreamer(session) as streamer:
            await streamer.subscribe(event_type, streamer_symbols)
            try:
                async with asyncio.timeout(timeout):
                    while len(events_by_symbol) < len(expected):
                        event = await streamer.get_event(event_type)
                        if event.event_symbol in expected:
                            events_by_symbol[event.event_symbol] = event
            except TimeoutError:
                timed_out = True
    except ExceptionGroup as eg:
        errors = "; ".join(f"{type(e).__name__}: {e}" for e in eg.exceptions)
        await raise_with_market_context(
            session, exchanges, ValueError(f"Streaming connection error for {sorted(expected)}: {errors}")
        )

    if timed_out:
        missing = expected - set(events_by_symbol)
        await raise_with_market_context(
            session,
            exchanges,
            ValueError(f"Timeout getting quotes after {timeout}s. No data received for: {sorted(missing)}"),
        )
    return [events_by_symbol[s] for s in streamer_symbols]


async def stream_multi_events(
    session: Session,
    event_types: list[type[Quote] | type[Greeks] | type[Summary]],
    streamer_symbols: list[str],
    timeout: float,
) -> dict[type, dict[str, Any]]:
    """Stream multiple event types concurrently on a single DXLink connection."""
    results: dict[type, dict[str, Any]] = {et: {} for et in event_types}
    expected = set(streamer_symbols)
    exchanges = exchanges_for_symbols(streamer_symbols)
    timed_out = False

    def all_complete() -> bool:
        return all(len(results[et]) >= len(expected) for et in event_types)

    try:
        async with DXLinkStreamer(session) as streamer:
            for et in event_types:
                await streamer.subscribe(et, streamer_symbols)
            try:
                async with asyncio.timeout(timeout):
                    pending_tasks: dict[type, asyncio.Task] = {}
                    while not all_complete():
                        for et in event_types:
                            if et not in pending_tasks and len(results[et]) < len(expected):
                                pending_tasks[et] = asyncio.ensure_future(streamer.get_event(et))

                        done, _ = await asyncio.wait(
                            pending_tasks.values(),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for task in done:
                            event = task.result()
                            for et in event_types:
                                if pending_tasks.get(et) is task:
                                    if event.event_symbol in expected:
                                        results[et][event.event_symbol] = event
                                    del pending_tasks[et]
                                    break
            except TimeoutError:
                timed_out = True
            finally:
                for task in pending_tasks.values():
                    task.cancel()
    except ExceptionGroup as eg:
        errors = "; ".join(f"{type(e).__name__}: {e}" for e in eg.exceptions)
        await raise_with_market_context(
            session, exchanges, ValueError(f"Streaming connection error for {sorted(expected)}: {errors}")
        )

    if timed_out:
        missing_info = {
            et.__name__: sorted(expected - set(results[et])) for et in event_types if len(results[et]) < len(expected)
        }
        await raise_with_market_context(
            session, exchanges, ValueError(f"Timeout after {timeout}s. Missing data: {missing_info}")
        )
    return results


async def stream_quotes_with_trade_fallback(
    session: Session,
    streamer_symbols: list[str],
    index_symbols: set[str],
    timeout: float,
) -> list[Quote | Trade]:
    """Stream quotes, falling back to Trade events for index symbols."""
    events_by_symbol: dict[str, Quote | Trade] = {}
    expected = set(streamer_symbols)
    exchanges = exchanges_for_symbols(streamer_symbols)
    timed_out = False
    try:
        async with DXLinkStreamer(session) as streamer:
            await streamer.subscribe(Quote, streamer_symbols)
            await streamer.subscribe(Trade, list(index_symbols))
            try:
                async with asyncio.timeout(timeout):
                    while len(events_by_symbol) < len(expected):
                        quote_task = asyncio.ensure_future(streamer.get_event(Quote))
                        trade_task = asyncio.ensure_future(streamer.get_event(Trade))
                        done, pending = await asyncio.wait(
                            [quote_task, trade_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for task in pending:
                            task.cancel()
                        for task in done:
                            event = task.result()
                            if event.event_symbol in expected:
                                if isinstance(event, Trade) and event.event_symbol in events_by_symbol:
                                    continue
                                events_by_symbol[event.event_symbol] = event
            except TimeoutError:
                timed_out = True
    except ExceptionGroup as eg:
        errors = "; ".join(f"{type(e).__name__}: {e}" for e in eg.exceptions)
        await raise_with_market_context(
            session, exchanges, ValueError(f"Streaming connection error for {sorted(expected)}: {errors}")
        )

    if timed_out:
        missing = expected - set(events_by_symbol)
        await raise_with_market_context(
            session,
            exchanges,
            ValueError(f"Timeout getting quotes after {timeout}s. No data received for: {sorted(missing)}"),
        )
    return [events_by_symbol[s] for s in streamer_symbols]
