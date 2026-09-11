from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import humanize
from tastytrade import Session
from tastytrade.dxfeed import Greeks, Quote, Trade
from tastytrade.market_sessions import ExchangeType, MarketStatus, get_market_sessions
from tastytrade.streamer import DXLinkStreamer


def exchanges_for_symbols(streamer_symbols: list[str]) -> set[ExchangeType]:
    exchanges: set[ExchangeType] = set()
    for sym in streamer_symbols:
        if sym.startswith("/") or sym.startswith("./"):
            if ":XCBF" in sym or sym.startswith(("/VX", "./VX")):
                exchanges.add(ExchangeType.CFE)
            else:
                exchanges.add(ExchangeType.CME)
        else:
            exchanges.add(ExchangeType.NYSE)
    return exchanges


def get_next_open_time(session, current_time: datetime) -> datetime | None:
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
    market_sessions = await get_market_sessions(session, list(exchanges))

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
    primary_error: ValueError,
) -> None:
    """Add market context without hiding the primary streaming error."""
    try:
        market_msg = await market_status_message(session, exchanges)
    except Exception as context_error:
        primary_error.add_note(f"Market-status lookup also failed: {type(context_error).__name__}: {context_error}")
        raise primary_error from context_error
    if market_msg:
        raise ValueError(market_msg) from primary_error
    raise primary_error


async def stream_events(
    session: Session,
    event_type: type[Quote] | type[Greeks],
    streamer_symbols: list[str],
    timeout: float,
) -> list[Any]:
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


async def stream_quotes_with_trade_fallback(
    session: Session,
    streamer_symbols: list[str],
    index_symbols: set[str],
    timeout: float,
) -> list[Quote | Trade]:
    """DXLink omits Quote events for some indices, so accept their Trade events."""
    events_by_symbol: dict[str, Quote | Trade] = {}
    expected = set(streamer_symbols)
    exchanges = exchanges_for_symbols(streamer_symbols)
    timed_out = False
    try:
        async with DXLinkStreamer(session) as streamer:
            await streamer.subscribe(Quote, streamer_symbols)
            await streamer.subscribe(Trade, list(index_symbols))
            # One in-flight get_event task per event type, kept across iterations: cancelling
            # the still-pending task each pass can discard an event the stream already
            # handed to it, which is never re-delivered.
            pending_by_type: dict[type[Quote] | type[Trade], asyncio.Task] = {}
            try:
                async with asyncio.timeout(timeout):
                    try:
                        while len(events_by_symbol) < len(expected):
                            for event_type in (Quote, Trade):
                                if event_type not in pending_by_type:
                                    pending_by_type[event_type] = asyncio.ensure_future(streamer.get_event(event_type))
                            done, _ = await asyncio.wait(pending_by_type.values(), return_when=asyncio.FIRST_COMPLETED)
                            for event_type, task in list(pending_by_type.items()):
                                if task not in done:
                                    continue
                                del pending_by_type[event_type]
                                event = task.result()
                                if event.event_symbol in expected:
                                    if isinstance(event, Trade) and event.event_symbol in events_by_symbol:
                                        continue
                                    events_by_symbol[event.event_symbol] = event
                    finally:
                        for task in pending_by_type.values():
                            task.cancel()
                        await asyncio.gather(*pending_by_type.values(), return_exceptions=True)
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
